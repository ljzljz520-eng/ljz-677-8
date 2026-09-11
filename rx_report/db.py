"""SQLite 持久化层：逐行记录报送状态与监管结果。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import FAILED, PENDING, SUCCESS, TRANSPORT

SCHEMA = """
CREATE TABLE IF NOT EXISTS prescription (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rx_no         TEXT NOT NULL UNIQUE,
    patient_id    TEXT NOT NULL,
    drug          TEXT NOT NULL,
    quantity      INTEGER NOT NULL CHECK (quantity > 0),
    fee           REAL NOT NULL CHECK (fee >= 0),
    status        TEXT NOT NULL DEFAULT 'PENDING',
    ack_code      TEXT,
    error_code    TEXT,
    error_message TEXT,
    batch_no      TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


class Repository:
    def __init__(self, db_path: str | Path = "rx_report.db"):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def init_db(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 导入 ----------------------------------------------------------------
    def upsert_pending(
        self, rx_no: str, patient_id: str, drug: str,
        quantity: int, fee: float,
    ) -> str:
        """插入新处方；已存在的处方（任何状态）一律不覆盖。

        返回 'inserted' 或 'duplicate'。绝不覆盖历史状态/结果。
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO prescription "
            "(rx_no, patient_id, drug, quantity, fee, status) "
            "VALUES (?,?,?,?,?,?)",
            (rx_no, patient_id, drug, quantity, fee, PENDING),
        )
        self.conn.commit()
        return "inserted" if cur.rowcount == 1 else "duplicate"

    # ---- 取数 ----------------------------------------------------------------
    def _rows(self, sql: str, params=()):
        from .models import Prescription
        return [
            Prescription(
                id=r["id"], rx_no=r["rx_no"], patient_id=r["patient_id"],
                drug=r["drug"], quantity=r["quantity"], fee=r["fee"],
                status=r["status"], ack_code=r["ack_code"],
                error_code=r["error_code"], error_message=r["error_message"],
                batch_no=r["batch_no"], attempts=r["attempts"],
            )
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def get(self, rx_no: str):
        rows = self._rows(
            "SELECT * FROM prescription WHERE rx_no = ?", (rx_no,))
        return rows[0] if rows else None

    def list_rows(self, status: str | None = None, limit: int | None = None):
        sql = "SELECT * FROM prescription"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self._rows(sql, params)

    def candidates(self, exclude_success: bool, failed_only: bool,
                   exclude_rxnums: set[str] | None = None,
                   batch_size: int | None = None):
        """选取待报送行。

        - exclude_success=True（常规报送）：PENDING / FAILED / TRANSPORT_FAILED
          都可送，SUCCESS 永不取出，从根上杜绝成功行被重复报送。
        - failed_only=True（重送）：只取 FAILED / TRANSPORT_FAILED。
        exclude_rxnums 用于本次运行内排除已处理行，防同轮循环重送。
        """
        if failed_only:
            statuses = [FAILED, TRANSPORT]
        elif exclude_success:
            statuses = [PENDING, FAILED, TRANSPORT]
        else:  # 保留兜底，正常流程不会走到
            statuses = [PENDING, FAILED, TRANSPORT, SUCCESS]
        placeholders = ",".join("?" * len(statuses))
        params: list = list(statuses)
        if exclude_rxnums:
            quoted = ",".join("?" * len(exclude_rxnums))
            params.extend(sorted(exclude_rxnums))
            sql = (f"SELECT * FROM prescription WHERE status IN ({placeholders}) "
                   f"AND rx_no NOT IN ({quoted}) ORDER BY id")
        else:
            sql = f"SELECT * FROM prescription WHERE status IN ({placeholders}) ORDER BY id"
        if batch_size is not None:
            sql += f" LIMIT {int(batch_size)}"
        return self._rows(sql, params)

    # ---- 结果回写 ------------------------------------------------------------
    def mark_business_result(self, rx_no: str, accepted: bool,
                             ack_code: str | None, error_code: str | None,
                             error_message: str | None, batch_no: str) -> None:
        """逐行业务结果回写。安全护栏：SUCCESS 行冻结，不允许任何覆盖。"""
        status = SUCCESS if accepted else FAILED
        cur = self.conn.execute(
            "UPDATE prescription SET "
            "status=CASE WHEN status='SUCCESS' THEN 'SUCCESS' ELSE ? END, "
            "ack_code=CASE WHEN status='SUCCESS' THEN ack_code ELSE ? END, "
            "error_code=CASE WHEN status='SUCCESS' THEN error_code ELSE ? END, "
            "error_message=CASE WHEN status='SUCCESS' THEN error_message ELSE ? END, "
            "batch_no=CASE WHEN status='SUCCESS' THEN batch_no ELSE ? END, "
            "attempts=CASE WHEN status='SUCCESS' THEN attempts ELSE attempts+1 END, "
            "updated_at=datetime('now','localtime') "
            "WHERE rx_no=?",
            (status, ack_code, error_code, error_message, batch_no, rx_no),
        )
        if cur.rowcount == 0:
            raise RuntimeError(f"处方 {rx_no} 不存在，无法回写结果")
        self.conn.commit()

    def mark_transport_failure(self, rx_nums: list[str], batch_no: str) -> None:
        """整批传输失败：不计业务成败，保留/转置为 TRANSPORT_FAILED，等待重送。

        成功行同样受护栏保护，不会被降级。
        """
        if not rx_nums:
            return
        qmarks = ",".join("?" * len(rx_nums))
        self.conn.execute(
            f"UPDATE prescription SET "
            "status=CASE WHEN status='SUCCESS' THEN 'SUCCESS' ELSE 'TRANSPORT_FAILED' END, "
            "error_code='TRANSPORT_ERROR', "
            "error_message='批次传输失败（监管端未受理），请重送', "
            "batch_no=?, attempts=attempts+1, "
            "updated_at=datetime('now','localtime') "
            f"WHERE rx_no IN ({qmarks})",
            [batch_no, *rx_nums],
        )
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM prescription GROUP BY status"
        ).fetchall()
        d = {PENDING: 0, SUCCESS: 0, FAILED: 0, TRANSPORT: 0}
        for r in rows:
            d[r["status"]] = r["c"]
        d["total"] = sum(d.values())
        return d

    def export_rows(self):
        return self._rows(
            "SELECT * FROM prescription ORDER BY id")

    def close(self) -> None:
        self.conn.close()
