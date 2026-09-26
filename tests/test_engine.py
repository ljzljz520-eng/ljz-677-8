"""报送引擎单元测试：使用假客户端，不启动网络服务。"""
import json
import os
import tempfile
import threading
import unittest

from app import config, db, regulator_client, reporter
from app.importer import import_batch

CSV = """处方号,患者编号,药品,数量,费用
RX001,P1,A,3,45.6
RX002,P2,B,2,28
RX002,P3,B,1,10
RX003,,C,1,5
RX004,P4,D,x,5
RX005,P5,E,600,96
RX006,P6,F,1,99999
RX007,P7,G,2,20
RX008,P8,H,1,12
"""


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_db = config.DB_PATH
        config.DB_PATH = os.path.join(self.tmp, "p.db")
        db.init_db(config.DB_PATH)
        with db.get_conn() as conn:
            self.bid, self.info = import_batch(conn, "t.csv", CSV.encode("utf-8"))

    def tearDown(self):
        config.DB_PATH = self._old_db

    def _client(self, behavior):
        """behavior: dict rx_no -> result-dict; 或 'transport' 抛传输错误。"""
        def call(records, batch_ref):
            if behavior == "transport":
                raise regulator_client.TransportError("boom", status=503)
            return [behavior[r["rx_no"]] for r in records]
        return call

    def _ok(self, rx):
        return {"rx_no": rx, "accepted": True, "regulator_id": "REG-" + rx,
                "error_code": None, "error_message": None, "retriable": False}

    def _fail(self, rx, retriable=False):
        return {"rx_no": rx, "accepted": False, "regulator_id": None,
                "error_code": "5001" if retriable else "4001",
                "error_message": "瞬时" if retriable else "超限",
                "retriable": retriable}

    def _statuses(self):
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT rx_no,status,attempts FROM prescriptions WHERE batch_id=? ORDER BY row_no",
                (self.bid,)).fetchall()
        out = {}
        for r in rows:
            # 重复处方号时保留首次出现的行（重复行单独用 SQL 查）
            out.setdefault(r["rx_no"], (r["status"], r["attempts"]))
        return out

    def test_01_import_validation(self):
        """本地校验：空患者/非数字数量/文件内重复处方号 → invalid，且不进入待报送。"""
        s = self._statuses()
        self.assertEqual(s["RX001"][0], "pending")
        self.assertEqual(s["RX002"][0], "pending")   # 首次出现的 RX002 有效
        self.assertEqual(s["RX003"][0], "invalid")   # 患者编号为空
        self.assertEqual(s["RX004"][0], "invalid")   # 数量非数字
        # 文件内重复处方号：第二个 RX002 无 rx 字段区分，按行号再查
        with db.get_conn() as conn:
            dup = conn.execute(
                "SELECT status,error_code FROM prescriptions WHERE rx_no='RX002' ORDER BY row_no").fetchall()
        self.assertEqual(dup[0]["status"], "pending")
        self.assertEqual(dup[1]["status"], "invalid")
        self.assertEqual(dup[1]["error_code"], "L_DUP_RX")
        # 超范围数值不属于本地校验，应放行到监管端
        self.assertEqual(s["RX005"][0], "pending")  # 600 数量
        self.assertEqual(s["RX006"][0], "pending")  # 99999 费用

    def test_02_submit_only_pending_and_writeback(self):
        """首次提交只发送 pending 行，结果逐行回写。"""
        behavior = {
            "RX001": self._ok("RX001"), "RX002": self._fail("RX002"),
            "RX005": self._fail("RX005"), "RX006": self._fail("RX006"),
            "RX007": self._fail("RX007", retriable=True),
            "RX008": self._ok("RX008"),
        }
        r = reporter.run(self.bid, mode="submit", client=self._client(behavior),
                         chunk_size=3)
        self.assertFalse(r["blocked"])
        self.assertEqual(r["rows_sent"], 6)   # 不含 3 条 invalid
        self.assertEqual(r["success"], 2)
        self.assertEqual(r["failed"], 4)
        s = self._statuses()
        self.assertEqual(s["RX001"][0], "success")
        self.assertEqual(s["RX001"][1], 1)
        self.assertEqual(s["RX002"][0], "failed")
        self.assertEqual(s["RX003"][0], "invalid")  # 绝不发送
        self.assertEqual(s["RX003"][1], 0)

    def test_03_retry_only_failed_no_duplicate(self):
        """重送只发 failed 行：success/invalid/pending 一律不重发，不产生重复。"""
        behavior1 = {
            "RX001": self._ok("RX001"), "RX002": self._fail("RX002"),
            "RX005": self._fail("RX005"), "RX006": self._fail("RX006"),
            "RX007": self._fail("RX007", retriable=True),
            "RX008": self._ok("RX008"),
        }
        reporter.run(self.bid, mode="submit", client=self._client(behavior1), chunk_size=3)

        sent_rx = []

        def client2(records, batch_ref):
            sent_rx.extend(r["rx_no"] for r in records)
            out = []
            for r in records:
                out.append(self._ok(r["rx_no"]) if r["rx_no"] == "RX007"
                           else self._fail(r["rx_no"]))
            return out

        r = reporter.run(self.bid, mode="retry_failed", client=client2)
        self.assertEqual(sorted(sent_rx), ["RX002", "RX005", "RX006", "RX007"])
        self.assertNotIn("RX001", sent_rx)   # 已成功 → 不重送
        self.assertEqual(r["success"], 1)
        self.assertEqual(r["failed"], 3)

        s = self._statuses()
        self.assertEqual(s["RX001"][1], 1)   # 成功行尝试次数不增加
        self.assertEqual(s["RX007"][1], 2)   # 失败后重送：两次尝试
        self.assertEqual(s["RX003"][1], 0)

        # 再次"提交待报送行"：没有 pending，0 行发送，不重复
        r2 = reporter.run(self.bid, mode="submit",
                          client=lambda recs, ref: (_ for _ in ()).throw(
                              AssertionError("不应再调用监管接口")))
        self.assertEqual(r2["rows_sent"], 0)

        # 只剩永久失败行时，再重送仍只发 failed
        r3 = reporter.run(self.bid, mode="retry_failed",
                          client=self._client({rx: self._fail(rx)
                                               for rx in ["RX002", "RX005", "RX006"]}))
        self.assertEqual(r3["rows_sent"], 3)

    def test_04_transport_error_marks_chunk_retriable(self):
        """chunk 传输级 503：该片所有行标 failed(E_RETRY)，可重送；其它片不受影响。"""
        calls = {"n": 0}

        def flaky_then_ok(records, batch_ref):
            calls["n"] += 1
            if calls["n"] == 1:
                raise regulator_client.TransportError("503", status=503)
            return [self._ok(r["rx_no"]) for r in records]

        r = reporter.run(self.bid, mode="submit", client=flaky_then_ok, chunk_size=3)
        # 6 个 pending，片大小 3：第1片 503 → 3 failed；第2片成功
        self.assertEqual(r["chunks_failed"], 1)
        s = self._statuses()
        self.assertEqual(s["RX001"][0], "failed")
        self.assertTrue(s["RX001"], )
        with db.get_conn() as conn:
            code = conn.execute(
                "SELECT error_code FROM prescriptions WHERE rx_no='RX001'").fetchone()["error_code"]
        self.assertEqual(code, "E_RETRY")
        self.assertEqual(s["RX007"][0], "success")  # 第二片成功

        # 重送失败的 3 行，全部恢复
        r2 = reporter.run(self.bid, mode="retry_failed", client=flaky_then_ok)
        self.assertEqual(r2["rows_sent"], 3)
        self.assertEqual(r2["success"], 3)

    def test_05_concurrent_submit_blocked(self):
        """并发第二次提交被批次锁拦截（防双击/防重复）。"""
        holder = threading.Event()
        release = threading.Event()

        def slow_client(records, batch_ref):
            holder.set()
            release.wait(timeout=5)
            return [self._ok(r["rx_no"]) for r in records]

        result = {}

        def run():
            result["second"] = reporter.run(self.bid, mode="submit",
                                            client=slow_client, chunk_size=2)

        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(holder.wait(timeout=5))
        first = reporter.run(self.bid, mode="submit", client=slow_client, chunk_size=2)
        self.assertTrue(first["blocked"])
        release.set()
        t.join()
        self.assertFalse(result["second"].get("blocked"))


if __name__ == "__main__":
    unittest.main()
