"""监管接口模拟器（独立 HTTP 服务，标准库实现）。

业务规则（确定性，便于演示/测试）:
  - 处方号重复提交已受理记录 -> 4091 DUPLICATE_RX 永久失败
  - 单次数量 > 500           -> 4001 QTY_EXCEED 永久失败
  - 单次费用 > 10000         -> 4002 FEE_EXCEED 永久失败
  - 空字段                   -> 4000 VALIDATION_ERROR 永久失败
  - 处方号以 7 结尾          -> 5001 TEMP_REJECT 瞬时失败(可重试；同一处方号
                               仅首次瞬时失败，再次提交恢复受理，模拟真实抖动)
  - 请求头 X-Mock-Flaky:1    -> 本次请求所有行 E_RETRY 瞬时失败(每次都触发)
  - 请求头 X-Mock-Outage:1   -> 首次请求返回 HTTP 503（一次性宕机），之后恢复
其余受理，生成受理号 REG-YYYYMMDD-XXXXXX。

POST /admin/reset 清空已受理处方号与一次性宕机标记（测试/演示用）。
"""
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config

STATE_DDL = """
CREATE TABLE IF NOT EXISTS accepted_rx (
    rx_no         TEXT PRIMARY KEY,
    regulator_id  TEXT NOT NULL,
    accepted_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS transient_fired (rx_no TEXT PRIMARY KEY, fired_at TEXT);
"""


class MockState:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.lock_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(STATE_DDL)
        self.conn.commit()
        self._seq = self.conn.execute("SELECT COUNT(*) c FROM accepted_rx").fetchone()[0]
        self._lock = __import__("threading").Lock()
        self._outage_fired = self.conn.execute(
            "SELECT v FROM kv WHERE k='outage_fired'").fetchone() is not None

    def reset(self):
        with self._lock:
            self.conn.execute("DELETE FROM accepted_rx")
            self.conn.execute("DELETE FROM kv")
            self.conn.execute("DELETE FROM transient_fired")
            self.conn.commit()
            self._seq = 0
            self._outage_fired = False

    def _next_id(self):
        self._seq += 1
        return f"REG-{time.strftime('%Y%m%d')}-{self._seq:06d}"

    def process(self, records, force_flaky=False, force_outage=False):
        """返回 (http_status, payload)。"""
        with self._lock:
            # 一次性整批宕机（chunk 级传输失败演示）
            if force_outage and not self._outage_fired:
                self._outage_fired = True
                self.conn.execute(
                    "INSERT OR REPLACE INTO kv(k,v) VALUES('outage_fired','1')")
                self.conn.commit()
                return 503, {"error": "REGULATOR_UNAVAILABLE",
                             "message": "监管平台维护中，请稍后重试"}

            results = []
            for rec in records:
                rx = (rec.get("rx_no") or "").strip()
                drug = (rec.get("drug") or "").strip()
                pid = (rec.get("patient_id") or "").strip()
                results.append(self._process_one(rec, rx, drug, pid, force_flaky))
            self.conn.commit()
            return 200, {"results": results}

    def _process_one(self, rec, rx, drug, pid, force_flaky):
        def fail(code, msg, retriable=False):
            return {"rx_no": rx, "accepted": False, "regulator_id": None,
                    "error_code": code, "error_message": msg, "retriable": retriable}

        if force_flaky:
            return fail("E_RETRY", "监管系统瞬时繁忙(模拟)", retriable=True)

        if not rx or not pid or not drug:
            return fail("4000", "必填项缺失（处方号/患者编号/药品）")

        try:
            qty = int(float(rec.get("quantity", 0)))
        except (TypeError, ValueError):
            qty = 0
        try:
            fee = float(rec.get("fee", 0))
        except (TypeError, ValueError):
            fee = 0.0

        if qty <= 0:
            return fail("4000", "数量必须为正整数")
        if qty > 500:
            return fail("4001", f"单次开药数量 {qty} 超过上限 500")
        if fee > 10000:
            return fail("4002", f"处方费用 {fee:.2f} 超过上限 10000")

        # 幂等：已受理的处方号不得重复入账
        row = self.conn.execute(
            "SELECT regulator_id FROM accepted_rx WHERE rx_no=?", (rx,)).fetchone()
        if row:
            return fail("4091", f"处方号 {rx} 已受理，禁止重复报送（受理号 {row[0]}）")

        if rx.endswith("7"):
            fired = self.conn.execute(
                "SELECT 1 FROM transient_fired WHERE rx_no=?", (rx,)).fetchone()
            if not fired:
                self.conn.execute(
                    "INSERT OR IGNORE INTO transient_fired(rx_no, fired_at) VALUES(?,?)",
                    (rx, time.strftime("%Y-%m-%d %H:%M:%S")))
                return fail("5001", "监管端瞬时校验失败，请重试", retriable=True)
            # 已瞬时失败过一次：本次恢复，继续走受理流程

        rid = self._next_id()
        self.conn.execute(
            "INSERT INTO accepted_rx(rx_no, regulator_id, accepted_at) VALUES(?,?,?)",
            (rx, rid, time.strftime("%Y-%m-%d %H:%M:%S")))
        return {"rx_no": rx, "accepted": True, "regulator_id": rid,
                "error_code": None, "error_message": None, "retriable": False}


STATE = None  # 由 make_server 注入


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockRegulator/1.0"

    def log_message(self, *args):  # 静默默认日志
        pass

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global STATE
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"

        if self.path == "/admin/reset":
            STATE.reset()
            self._json(200, {"ok": True})
            return

        if self.path != "/v1/prescriptions":
            self._json(404, {"error": "NOT_FOUND"})
            return

        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "BAD_JSON"})
            return

        delay = config.MOCK_LATENCY_MS
        try:
            delay = max(delay, int(self.headers.get("X-Mock-Delay-Ms", "0")))
        except ValueError:
            pass
        if delay:
            time.sleep(delay / 1000.0)

        status, out = STATE.process(
            payload.get("records", []),
            force_flaky=self.headers.get("X-Mock-Flaky") == "1",
            force_outage=self.headers.get("X-Mock-Outage") == "1",
        )
        self._json(status, out)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "mock-regulator"})
        else:
            self._json(404, {"error": "NOT_FOUND"})


def make_server(host=None, port=None, db_path=None):
    global STATE
    STATE = MockState(db_path if db_path is not None else config.MOCK_DB_PATH)
    httpd = ThreadingHTTPServer(
        (host if host is not None else config.MOCK_HOST,
         port if port is not None else config.MOCK_PORT), _Handler)
    return httpd
