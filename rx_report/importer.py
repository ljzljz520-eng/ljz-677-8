"""CSV 导入：处方号,患者编号,药品,数量,费用（支持中文表头）。"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

HEADER_ALIASES = {
    "处方号": "rx_no",
    "处方编号": "rx_no",
    "患者编号": "patient_id",
    "患者ID": "patient_id",
    "药品": "drug",
    "药品名称": "drug",
    "数量": "qty",
    "数量(盒)": "qty",
    "费用": "fee",
    "费用(元)": "fee",
    "金额": "fee",
}
REQUIRED = ["rx_no", "patient_id", "drug", "qty", "fee"]


@dataclass
class ImportedRow:
    line_no: int
    rx_no: str
    patient_id: str
    drug: str
    quantity: int
    fee: float


@dataclass
class ImportReport:
    inserted: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_qty(value: str, line_no: int) -> int:
    v = value.strip()
    try:
        q = int(v)
    except ValueError as e:
        raise ValueError(f"第{line_no}行 数量 '{value}' 不是整数") from e
    if q <= 0:
        raise ValueError(f"第{line_no}行 数量 {q} 必须为正整数")
    return q


def _parse_fee(value: str, line_no: int) -> float:
    v = value.strip()
    try:
        f = float(v)
    except ValueError as e:
        raise ValueError(f"第{line_no}行 费用 '{value}' 不是数字") from e
    if f < 0:
        raise ValueError(f"第{line_no}行 费用 {f} 不能为负")
    return f


def load_csv(path: str | Path, repo) -> ImportReport:
    """读取 CSV 并落库为 PENDING；脏数据行拒绝并记录，不影响其他行。"""
    report = ImportReport()
    seen_in_file: dict[str, int] = {}
    path = Path(path)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    if not rows:
        report.errors.append("文件为空")
        return report

    raw_header = [h.strip() for h in rows[0]]
    cols = {}
    for idx, h in enumerate(raw_header):
        key = HEADER_ALIASES.get(h)
        if key:
            cols[key] = idx
    missing = [k for k in REQUIRED if k not in cols]
    if missing:
        cn = {"rx_no": "处方号", "patient_id": "患者编号", "drug": "药品",
              "qty": "数量", "fee": "费用"}
        report.errors.append(
            "表头缺少必需列: " + ", ".join(cn[m] for m in missing))
        report.rejected = max(len(rows) - 1, 0)
        return report

    for offset, cells in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in cells):
            continue  # 跳过空行
        try:
            if len(cells) <= max(cols.values()):
                raise ValueError(f"第{offset}行 列数不足")
            rx_no = cells[cols["rx_no"]].strip()
            patient_id = cells[cols["patient_id"]].strip()
            drug = cells[cols["drug"]].strip()
            qty = _parse_qty(cells[cols["qty"]], offset)
            fee = _parse_fee(cells[cols["fee"]], offset)

            if not rx_no:
                raise ValueError(f"第{offset}行 处方号为空")
            if not patient_id:
                raise ValueError(f"第{offset}行 患者编号为空")
            if not drug:
                raise ValueError(f"第{offset}行 药品为空")
            if rx_no in seen_in_file:
                raise ValueError(
                    f"第{offset}行 处方号 {rx_no} 与第{seen_in_file[rx_no]}行重复")
            seen_in_file[rx_no] = offset

            outcome = repo.upsert_pending(rx_no, patient_id, drug, qty, fee)
            if outcome == "inserted":
                report.inserted += 1
            else:
                report.duplicates += 1
        except ValueError as e:
            report.rejected += 1
            report.errors.append(str(e))
    return report
