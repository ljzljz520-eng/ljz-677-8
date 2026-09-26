"""端到端测试：真实启动监管端模拟器 + 平台 HTTP 服务，走完整 REST 流程。"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

from app import config, db
from app.webapp import make_server as make_platform
from app.mock_regulator import make_server as make_regulator

CSV = """处方号,患者编号,药品,数量,费用
RXE001,P1,阿莫西林,3,45.6
RXE002,P2,布洛芬,2,28
RXE003,P3,硝苯地平,5,120.5
RXE004,P4,阿托伐他汀,3,150
RXE005,P5,奥美拉唑,12,12800
RXE006,P6,二甲双胍,600,96
RXE007,P7,氯雷他定,2,36
RXE008,,多潘立酮,2,25
RXE009,P9,,2,30
"""


def _http(method, url, body=None, headers=None, raw=False):
    data = None
    h = headers or {}
    if body is not None:
        data = body if raw else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            return resp.status, payload
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class E2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls._orig = (config.DB_PATH, config.MOCK_DB_PATH, config.REGULATOR_URL)
        config.DB_PATH = os.path.join(cls.tmp, "p.db")
        config.MOCK_DB_PATH = os.path.join(cls.tmp, "reg.db")
        db.init_db(config.DB_PATH)

        cls.reg = make_regulator(host="127.0.0.1", port=0)
        cls.rport = cls.reg.server_address[1]
        threading.Thread(target=cls.reg.serve_forever, daemon=True).start()
        config.REGULATOR_URL = f"http://127.0.0.1:{cls.rport}/v1/prescriptions"

        cls.plat = make_platform(host="127.0.0.1", port=0)
        cls.pport = cls.plat.server_address[1]
        threading.Thread(target=cls.plat.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.pport}"
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.reg.shutdown()
        cls.plat.shutdown()
        config.DB_PATH, config.MOCK_DB_PATH, config.REGULATOR_URL = cls._orig

    def setUp(self):
        _http("POST", f"http://127.0.0.1:{self.rport}/admin/reset")
        st, body = _http("POST", self.base + "/api/batches",
                         body=CSV.encode("utf-8"), raw=True,
                         headers={"Content-Type": "text/csv; charset=utf-8",
                                  "X-Filename": "e2e.csv"})
        self.assertEqual(st, 201, body)
        self.bid = json.loads(body)["batch_id"]

    def _detail(self):
        st, body = _http("GET", f"{self.base}/api/batches/{self.bid}")
        self.assertEqual(st, 200)
        d = json.loads(body)
        return {r["rx_no"] or f"row{r['row_no']}": r for r in d["rows"]}, d["batch"]

    def test_01_full_flow(self):
        rows, batch = self._detail()
        self.assertEqual(batch["total_rows"], 9)
        self.assertEqual(rows["RXE008"]["status"], "invalid")  # 患者编号空
        self.assertEqual(rows["RXE009"]["status"], "invalid")  # 药品空
        pending = [rx for rx, r in rows.items() if r["status"] == "pending"]
        self.assertEqual(sorted(pending),
                         ["RXE001", "RXE002", "RXE003", "RXE004",
                          "RXE005", "RXE006", "RXE007"])

        # 首次提交，片大小 3
        st, body = _http("POST", f"{self.base}/api/batches/{self.bid}/submit",
                         body={"chunk_size": 3})
        self.assertEqual(st, 200, body)
        r = json.loads(body)
        self.assertEqual(r["rows_sent"], 7)
        self.assertEqual(r["success"], 4)   # 001-004 受理
        # RXE007 以 7 结尾：首次 5001 瞬时失败；005 费用超限、006 数量超限永久失败
        self.assertEqual(r["failed"], 3)
        self.assertFalse(r["blocked"])

        rows, batch = self._detail()
        self.assertEqual(rows["RXE001"]["status"], "success")
        self.assertIsNotNone(rows["RXE001"]["regulator_id"])
        self.assertEqual(rows["RXE005"]["error_code"], "4002")
        self.assertEqual(rows["RXE006"]["error_code"], "4001")
        self.assertEqual(rows["RXE007"]["status"], "failed")
        self.assertEqual(rows["RXE007"]["error_code"], "5001")
        self.assertEqual(rows["RXE007"]["attempts"], 1)
        self.assertEqual(batch["status"], "completed_with_errors")

        # 重复点"提交待报送"：没有 pending，0 发送，不产生重复
        st, body = _http("POST", f"{self.base}/api/batches/{self.bid}/submit", body={})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["rows_sent"], 0)

        # 只重送失败行：007 第二次恢复成功；005/006 永久失败依旧
        st, body = _http("POST", f"{self.base}/api/batches/{self.bid}/retry", body={})
        self.assertEqual(st, 200, body)
        r = json.loads(body)
        self.assertEqual(r["rows_sent"], 3)
        self.assertEqual(r["success"], 1)
        self.assertEqual(r["failed"], 2)

        rows, _ = self._detail()
        self.assertEqual(rows["RXE007"]["status"], "success")
        self.assertEqual(rows["RXE007"]["attempts"], 2)
        self.assertEqual(rows["RXE001"]["attempts"], 1)  # 首次成功的未再发送
        self.assertEqual(rows["RXE005"]["status"], "failed")

        # 再次重送：只发 005/006，不发 007/001
        st, body = _http("POST", f"{self.base}/api/batches/{self.bid}/retry", body={})
        r = json.loads(body)
        self.assertEqual(r["rows_sent"], 2)

    def test_02_duplicate_rx_rejected_by_regulator(self):
        """监管端幂等：第二个批次提交已受理处方号 → 4091，逐行回写失败。"""
        _http("POST", f"{self.base}/api/batches/{self.bid}/submit", body={"chunk_size": 50})
        # 再导入包含相同处方号的新文件
        dup_csv = "处方号,患者编号,药品,数量,费用\nRXE001,P1,阿莫西林,3,45.6\nRXE010,P10,新药品,1,10\n"
        st, body = _http("POST", self.base + "/api/batches", body=dup_csv.encode(), raw=True,
                         headers={"Content-Type": "text/csv", "X-Filename": "dup.csv"})
        bid2 = json.loads(body)["batch_id"]
        st, body = _http("POST", f"{self.base}/api/batches/{bid2}/submit", body={})
        self.assertEqual(st, 200)
        r = json.loads(body)
        self.assertEqual(r["success"], 1)
        self.assertEqual(r["failed"], 1)
        st, body = _http("GET", f"{self.base}/api/batches/{bid2}")
        rows = {x["rx_no"]: x for x in json.loads(body)["rows"]}
        self.assertEqual(rows["RXE001"]["error_code"], "4091")
        self.assertIn("禁止重复报送", rows["RXE001"]["error_message"])
        self.assertEqual(rows["RXE010"]["status"], "success")

    def test_03_chunk_outage_then_retry(self):
        """片级 503：第一片全部 E_RETRY 失败，重送后全部成功，成功片不重送。"""
        st, body = _http("POST", f"{self.base}/api/batches/{self.bid}/submit",
                         body={"chunk_size": 3, "force_outage": True})
        r = json.loads(body)
        self.assertEqual(r["chunks_failed"], 1)
        rows, _ = self._detail()
        # 第一片 RXE001-003 失败且可重试
        for rx in ("RXE001", "RXE002", "RXE003"):
            self.assertEqual(rows[rx]["status"], "failed")
            self.assertEqual(rows[rx]["error_code"], "E_RETRY")
        # 后续片已正常处理：RXE007 首次瞬时失败
        self.assertEqual(rows["RXE004"]["status"], "success")

        # 宕机一次性；重送失败行，全部恢复（RXE007 不在失败第一片中，它在成功片里失败为5001）
        st, body = _http("POST", f"{self.base}/api/batches/{self.bid}/retry", body={})
        r = json.loads(body)
        # 失败行 = 第一片3条 + RXE005 + RXE006 + RXE007
        self.assertEqual(r["rows_sent"], 6)
        self.assertEqual(r["success"], 4)  # 001,002,003 恢复 + 007 第二次恢复
        self.assertEqual(r["failed"], 2)   # 005,006 永久失败
        rows, _ = self._detail()
        for rx in ("RXE001", "RXE002", "RXE003", "RXE007"):
            self.assertEqual(rows[rx]["status"], "success", rx)
        self.assertEqual(rows["RXE004"]["attempts"], 1)  # 成功片未重发

    def test_04_export_csv_and_audit(self):
        _http("POST", f"{self.base}/api/batches/{self.bid}/submit", body={"chunk_size": 3})
        st, body = _http("GET", f"{self.base}/api/batches/{self.bid}/export.csv")
        self.assertEqual(st, 200)
        text = body.decode("utf-8-sig")
        self.assertIn("报送状态", text)
        self.assertIn("监管受理号", text)
        self.assertIn("已受理", text)
        self.assertIn("本地无效", text)

        st, body = _http("GET", f"{self.base}/api/batches/{self.bid}/audit")
        events = [e["event"] for e in json.loads(body)["log"]]
        self.assertIn("import", events)
        self.assertIn("submit", events)
        self.assertIn("success", events)
        self.assertIn("failed", events)


if __name__ == "__main__":
    unittest.main()
