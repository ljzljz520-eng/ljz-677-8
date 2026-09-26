# -*- coding: utf-8 -*-
"""
门诊处方数据报送台
- 导入处方明细行（处方号/患者编号/药品/数量/费用）
- 分批提交到监管接口，逐行回写结果
- 只允许重送失败行；成功行在 DB 约束 + 服务端校验 + 监管侧幂等三重保护下不会重复报送
运行: python3 server.py [port]   默认 8000
"""
import csv
import io
import json
import os
import sqlite3
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import regulatory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "rx_upload.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 报送临界区：保证"认领行 -> 报送 -> 回写"不被并发请求打乱
SUBMIT_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS rx_lines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,          -- 幂等键: 处方号#行号
    rx_no           TEXT NOT NULL,                 -- 处方号
    line_no         INTEGER NOT NULL,              -- 处方内行号
    patient_id      TEXT NOT NULL DEFAULT '',      -- 患者编号
    drug            TEXT NOT NULL,                 -- 药品
    quantity        REAL NOT NULL,                 -- 数量
    fee             REAL NOT NULL,                 -- 费用
    status          TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING','SUBMITTING','SUCCESS','FAILED')),
    last_code       TEXT,                          -- 最近一次监管返回码
    last_error      TEXT,                          -- 异常回写（逐行）
    batch_no        TEXT,                          -- 最近批次号
    attempts        INTEGER NOT NULL DEFAULT 0,    -- 已尝试次数
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no      TEXT NOT NULL UNIQUE,
    total         INTEGER NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,      -- 请求中被拒绝的行（已成功/报送中）
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    with conn:
        conn.executescript(SCHEMA)
        regulatory.ensure_ledger(conn)
    conn.close()


# ---------------------------------------------------------------- 导入
CSV_HEADER_MAP = {  # 中文表头 -> 内部字段
    "处方号": "rx_no", "患者编号": "patient_id", "药品": "drug",
    "数量": "quantity", "费用": "fee", "行号": "line_no",
}


def normalize_row(r):
    """把中文表头的行映射成内部字段名，同时兼容英文字段名。"""
    out = {}
    for k, v in r.items():
        if k is None:
            continue
        key = CSV_HEADER_MAP.get(str(k).strip().lstrip("﻿"), str(k).strip())
        out[key] = v
    return out


def import_rows(conn, rows):
    """
    导入明细行。幂等键 = 处方号#行号：
    - 源文件带「行号」列时直接使用；
    - 否则按处方号在"本文件"中的出现顺序编号（第1行、第2行…）。
    因此同一文件重复导入会得到相同幂等键，被 INSERT OR IGNORE 去重。
    """
    inserted, duplicated, invalid = 0, 0, []
    seq_in_file = {}
    for idx, raw in enumerate(rows, start=1):
        r = normalize_row(raw)
        rx_no = str(r.get("rx_no", "")).strip()
        drug = str(r.get("drug", "")).strip()
        try:
            quantity = float(r.get("quantity"))
            fee = float(r.get("fee"))
        except (TypeError, ValueError):
            invalid.append(f"第{idx}行: 数量/费用不是数字")
            continue
        if not rx_no or not drug:
            invalid.append(f"第{idx}行: 处方号或药品为空")
            continue
        line_no_raw = str(r.get("line_no", "")).strip()
        if line_no_raw:
            line_no = int(line_no_raw)
        else:
            seq_in_file[rx_no] = seq_in_file.get(rx_no, 0) + 1
            line_no = seq_in_file[rx_no]
        key = f"{rx_no}#{line_no}"
        cur = conn.execute(
            """INSERT OR IGNORE INTO rx_lines
               (idempotency_key, rx_no, line_no, patient_id, drug, quantity, fee)
               VALUES (?,?,?,?,?,?,?)""",
            (key, rx_no, line_no, str(r.get("patient_id", "")).strip(),
             drug, quantity, fee),
        )
        if cur.rowcount:
            inserted += 1
        else:
            duplicated += 1
    return {"inserted": inserted, "duplicated": duplicated, "invalid": invalid}


# ---------------------------------------------------------------- 报送
def submit_lines(conn, line_ids):
    """
    分批报送核心。只允许 PENDING / FAILED 状态的行进入批次：
    - SUCCESS / SUBMITTING 行会被拒绝并计入 skipped，绝不重复报送；
    - 认领用条件 UPDATE 原子完成，防止并发重复认领；
    - 监管侧按幂等键二次去重（见 regulatory.py）。
    """
    batch_no = uuid.uuid4().hex[:12].upper()
    result = {"batch_no": batch_no, "total": 0, "success": 0,
              "failed": 0, "skipped": [], "rows": []}
    if not line_ids:
        result["error"] = "没有可报送的行"
        return result

    ph = ",".join("?" * len(line_ids))
    rows = conn.execute(
        f"SELECT * FROM rx_lines WHERE id IN ({ph})", line_ids
    ).fetchall()

    claimable, skipped = [], []
    for r in rows:
        if r["status"] in ("PENDING", "FAILED"):
            claimable.append(r)
        else:
            skipped.append({"id": r["id"], "rx_no": r["rx_no"],
                            "status": r["status"],
                            "reason": "已受理或报送中，禁止重复报送"})
    result["skipped"] = skipped
    if not claimable:
        result["error"] = "所选行均不可报送（仅待报送/失败行可提交）"
        return result

    # 原子认领：状态 -> SUBMITTING，并记录批次号
    ids = [r["id"] for r in claimable]
    conn.execute(
        f"UPDATE rx_lines SET status='SUBMITTING', batch_no=?, "
        f"updated_at=datetime('now','localtime') "
        f"WHERE id IN ({','.join('?'*len(ids))}) AND status IN ('PENDING','FAILED')",
        [batch_no] + ids,
    )
    claimed = conn.execute(
        f"SELECT * FROM rx_lines WHERE batch_no=? AND status='SUBMITTING'",
        (batch_no,),
    ).fetchall()

    conn.execute(
        "INSERT INTO batches (batch_no, total, skipped_count) VALUES (?,?,?)",
        (batch_no, len(claimed), len(skipped)),
    )

    # ---- 调用监管接口 ----
    items = [dict(r) for r in claimed]
    results = regulatory.submit_batch(conn, items)

    # ---- 逐行回写 ----
    succ = fail = 0
    for it in items:
        res = results[it["idempotency_key"]]
        if res["ok"]:
            succ += 1
            conn.execute(
                """UPDATE rx_lines SET status='SUCCESS', last_code=?,
                   last_error=?, attempts=attempts+1,
                   updated_at=datetime('now','localtime') WHERE id=?""",
                (res["code"], res["message"], it["id"]),
            )
        else:
            fail += 1
            conn.execute(
                """UPDATE rx_lines SET status='FAILED', last_code=?,
                   last_error=?, attempts=attempts+1,
                   updated_at=datetime('now','localtime') WHERE id=?""",
                (res["code"], f'[{res["code"]}] {res["message"]}', it["id"]),
            )
        result["rows"].append({
            "id": it["id"], "rx_no": it["rx_no"], "drug": it["drug"],
            "ok": res["ok"], "code": res["code"], "message": res["message"],
        })

    conn.execute(
        "UPDATE batches SET success_count=?, fail_count=? WHERE batch_no=?",
        (succ, fail, batch_no),
    )
    result.update(total=len(claimed), success=succ, failed=fail)
    return result


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n).decode("utf-8") if n else ""

    def log_message(self, *a):  # 静音访问日志
        pass

    # ---------------- GET ----------------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        conn = get_db()
        try:
            if u.path == "/api/lines":
                q = parse_qs(u.query).get("status", ["ALL"])[0]
                sql = "SELECT * FROM rx_lines"
                args = []
                if q != "ALL":
                    sql += " WHERE status=?"
                    args.append(q)
                sql += " ORDER BY id"
                rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
                self._json({"rows": rows})
            elif u.path == "/api/stats":
                rows = conn.execute(
                    "SELECT status, COUNT(*) c FROM rx_lines GROUP BY status"
                ).fetchall()
                stats = {"PENDING": 0, "SUBMITTING": 0, "SUCCESS": 0, "FAILED": 0}
                stats.update({r["status"]: r["c"] for r in rows})
                self._json(stats)
            elif u.path == "/api/batches":
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM batches ORDER BY id DESC LIMIT 50").fetchall()]
                self._json({"batches": rows})
            else:
                self._json({"error": "not found"}, 404)
        finally:
            conn.close()

    # ---------------- POST ----------------
    def do_POST(self):
        u = urlparse(self.path)
        try:
            payload = json.loads(self._body() or "{}")
        except json.JSONDecodeError:
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return

        if u.path == "/api/import":
            rows = payload.get("rows")
            if rows is None and "csv" in payload:
                reader = csv.DictReader(io.StringIO(payload["csv"]))
                rows = list(reader)
            if not rows:
                self._json({"error": "没有可导入的数据"}, 400)
                return
            conn = get_db()
            try:
                with conn:
                    res = import_rows(conn, rows)
                self._json(res)
            finally:
                conn.close()

        elif u.path == "/api/batches":
            line_ids = payload.get("line_ids") or []
            if not isinstance(line_ids, list) or not line_ids:
                self._json({"error": "line_ids 不能为空"}, 400)
                return
            line_ids = [int(x) for x in line_ids]
            with SUBMIT_LOCK:              # 串行化报送，避免并发批次互相干扰
                conn = get_db()
                try:
                    with conn:
                        res = submit_lines(conn, line_ids)
                    self._json(res)
                finally:
                    conn.close()
        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    init_db()
    print(f"门诊处方数据报送台: http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
