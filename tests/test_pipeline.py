"""端到端测试：纯标准库，python -m unittest 即可运行。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rx_report.db import Repository
from rx_report.exporter import export_csv
from rx_report.importer import load_csv
from rx_report.models import FAILED, PENDING, SUCCESS, TRANSPORT
from rx_report.regulator import (E_DRUG_UNKNOWN, E_FEE_LIMIT,
                                 E_PATIENT_FORMAT, RegulatorClient)
from rx_report.service import submit

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "prescriptions.csv"


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.repo = Repository(self.db)
        self.repo.init_db()

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_import_validates_and_dedups(self):
        rep = load_csv(SAMPLE, self.repo)
        # 8 行落库（数量0、费用非数字 2 行被拒绝）
        self.assertEqual(rep.inserted, 8)
        self.assertEqual(rep.rejected, 2)
        # 再次导入：全部重复跳过，不覆盖任何状态
        rep2 = load_csv(SAMPLE, self.repo)
        self.assertEqual(rep2.duplicates, 8)
        self.assertEqual(rep2.inserted, 0)

    def test_submit_mixed_batch_writes_per_row(self):
        load_csv(SAMPLE, self.repo)
        client = RegulatorClient()
        s = submit(self.repo, client, batch_size=3)
        # 5 成功，3 业务失败
        self.assertEqual(s.success, 5)
        self.assertEqual(s.failed, 3)
        self.assertEqual(s.total, 8)

        ok = self.repo.get("RX0001")
        self.assertEqual(ok.status, SUCCESS)
        self.assertIsNotNone(ok.ack_code)
        self.assertEqual(ok.attempts, 1)

        bad_pid = self.repo.get("RX0003")
        self.assertEqual(bad_pid.status, FAILED)
        self.assertEqual(bad_pid.error_code, E_PATIENT_FORMAT)

        bad_drug = self.repo.get("RX0004")
        self.assertEqual(bad_drug.status, FAILED)
        self.assertEqual(bad_drug.error_code, E_DRUG_UNKNOWN)

        fee_over = self.repo.get("RX0007")
        self.assertEqual(fee_over.status, FAILED)
        self.assertEqual(fee_over.error_code, E_FEE_LIMIT)

    def test_retry_failed_only_and_transient_recovery(self):
        load_csv(SAMPLE, self.repo)
        # RX0009 第一次随批到达时制造一次传输故障
        client = RegulatorClient(flaky_rxnums=("RX0009",))
        submit(self.repo, client, batch_size=3)

        r9 = self.repo.get("RX0009")
        r10 = self.repo.get("RX0010")
        self.assertEqual(r9.status, TRANSPORT)
        self.assertEqual(r10.status, TRANSPORT)

        success_before = {
            r.rx_no for r in self.repo.list_rows(SUCCESS)}
        delivered_rx = [
            n for b in client.delivered_batches for n in b["rx_nums"]]
        self.assertNotIn("RX0001", delivered_rx[3:])  # 后续批次不含成功行

        # 只重送失败/传输失败行
        s2 = submit(self.repo, client, batch_size=3, failed_only=True)
        self.assertEqual(s2.total, 5)  # 3 永久失败 + 2 传输失败
        # 瞬时故障恢复：RX0009/RX0010 成功
        self.assertEqual(self.repo.get("RX0009").status, SUCCESS)
        self.assertEqual(self.repo.get("RX0010").status, SUCCESS)
        # 永久失败仍是失败，错误回写到行
        self.assertEqual(self.repo.get("RX0003").status, FAILED)
        # 已成功行没被重送、没被覆盖
        success_after = {
            r.rx_no for r in self.repo.list_rows(SUCCESS)}
        self.assertTrue(success_before.issubset(success_after))
        all_delivered = [
            n for b in client.delivered_batches for n in b["rx_nums"]]
        # RX0001 全程只到达监管端一次
        self.assertEqual(all_delivered.count("RX0001"), 1)

    def test_full_batch_transport_failure_marks_nothing_business(self):
        load_csv(SAMPLE, self.repo)
        client = RegulatorClient(flaky_rate=1.0, seed=1)
        s = submit(self.repo, client, batch_size=3)
        self.assertEqual(s.success, 0)
        self.assertEqual(s.failed, 0)
        self.assertEqual(s.transport_failed, 8)
        self.assertTrue(
            all(r.status == TRANSPORT
                for r in self.repo.list_rows()))
        # 监管端实际未受理任何批次
        self.assertEqual(client.delivered_batches, [])

    def test_resubmit_after_full_transport_failure_is_idempotent(self):
        load_csv(SAMPLE, self.repo)
        bad = RegulatorClient(flaky_rate=1.0, seed=1)
        submit(self.repo, bad, batch_size=4)
        # 换用健康接口重送
        good = RegulatorClient()
        s = submit(self.repo, good, batch_size=4, failed_only=True)
        self.assertEqual(s.success, 5)
        self.assertEqual(s.failed, 3)
        self.assertEqual(s.transport_failed, 0)
        # 再次“全量报送”不应重送任何已成功行
        s3 = submit(self.repo, good, batch_size=4)
        self.assertEqual(s3.success, 0)
        delivered = [n for b in good.delivered_batches for n in b["rx_nums"]]
        for n in ("RX0001", "RX0002", "RX0008", "RX0009", "RX0010"):
            self.assertEqual(delivered.count(n), 1)

    def test_export_contains_per_row_results(self):
        load_csv(SAMPLE, self.repo)
        submit(self.repo, RegulatorClient(), batch_size=2)
        out = Path(self.tmp.name) / "result.csv"
        n = export_csv(self.repo, out)
        self.assertEqual(n, 8)
        text = out.read_text(encoding="utf-8-sig")
        self.assertIn("报送状态", text)
        self.assertIn("ACK-RX0001", text)
        self.assertIn("E_PATIENT_ID", text)

    def test_success_row_cannot_be_overwritten(self):
        load_csv(SAMPLE, self.repo)
        client = RegulatorClient()
        submit(self.repo, client, batch_size=10)
        ack = self.repo.get("RX0001").ack_code
        # 直接尝试把成功行回写成失败，护栏必须拦住
        self.repo.mark_business_result(
            "RX0001", False, None, "X", "手工篡改", "Bbad")
        r = self.repo.get("RX0001")
        self.assertEqual(r.status, SUCCESS)
        self.assertEqual(r.ack_code, ack)
        self.assertIsNone(r.error_code)


if __name__ == "__main__":
    unittest.main()
