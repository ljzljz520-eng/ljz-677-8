"""导出报送结果到 CSV（含状态、回执、错误码、错误信息、批次号、尝试次数）。

使用 utf-8-sig，Excel 直接打开中文不乱码。
"""
from __future__ import annotations

import csv
from pathlib import Path

from .models import STATUS_LABELS

HEADER = [
    "处方号", "患者编号", "药品", "数量", "费用",
    "报送状态", "监管回执", "错误码", "错误信息", "批次号", "尝试次数",
]


def export_csv(repo, path: str | Path) -> int:
    rows = repo.export_rows()
    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for r in rows:
            w.writerow([
                r.rx_no, r.patient_id, r.drug, r.quantity,
                f"{r.fee:.2f}", STATUS_LABELS.get(r.status, r.status),
                r.ack_code or "", r.error_code or "",
                r.error_message or "", r.batch_no or "", r.attempts,
            ])
    return len(rows)
