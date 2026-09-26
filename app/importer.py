"""处方 CSV 导入与本地校验。

支持的表头(中英文均可，顺序不强制):
  处方号/处方编号/rx_no | 患者编号/患者ID/patient_id | 药品/药品名称/drug
  数量/quantity | 费用/fee
"""
import csv
import io
from . import db

# 表头别名 -> 标准字段
ALIASES = {
    "rx_no": {"处方号", "处方编号", "处方号码", "rx_no", "rxno", "rx"},
    "patient_id": {"患者编号", "患者id", "患者号", "病人编号", "patient_id", "patientid"},
    "drug": {"药品", "药品名称", "药品名", "drug", "medicine", "drug_name"},
    "quantity": {"数量", "quantity", "qty"},
    "fee": {"费用", "金额", "fee", "cost", "amount"},
}


def _decode(data):
    """兼容带/不带 BOM 的 UTF-8 以及 GBK。"""
    if isinstance(data, str):
        return data
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_csv(data):
    """返回 (records, header_error)。records 为 dict 列表。"""
    text = _decode(data)
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], "文件为空"

    header = [c.strip() for c in rows[0]]
    col_map = {}
    for idx, name in enumerate(header):
        key = name.lower().replace(" ", "")
        for std, names in ALIASES.items():
            if key in {n.lower().replace(" ", "") for n in names}:
                col_map[std] = idx
    missing = [f for f in ("rx_no", "patient_id", "drug", "quantity", "fee") if f not in col_map]
    if missing:
        cn = {"rx_no": "处方号", "patient_id": "患者编号", "drug": "药品",
              "quantity": "数量", "fee": "费用"}
        return [], "缺少必需列: " + "、".join(cn[m] for m in missing)

    records = []
    for raw in rows[1:]:
        def get(f):
            i = col_map[f]
            return raw[i].strip() if i < len(raw) else ""
        records.append({
            "rx_no": get("rx_no"),
            "patient_id": get("patient_id"),
            "drug": get("drug"),
            "quantity": get("quantity"),
            "fee": get("fee"),
            "_extra": raw,
        })
    return records, None


def _validate_row(rec):
    """本地校验。返回错误码/消息，None 表示通过(进入待报送)。"""
    if not rec["rx_no"]:
        return "L_EMPTY_RX", "处方号为空"
    if not rec["patient_id"]:
        return "L_EMPTY_PATIENT", "患者编号为空"
    if not rec["drug"]:
        return "L_EMPTY_DRUG", "药品名称为空"
    try:
        q = int(rec["quantity"])
        if q <= 0:
            return "L_QTY_INVALID", "数量必须为正整数"
    except ValueError:
        return "L_QTY_INVALID", "数量不是有效整数"
    try:
        f = float(rec["fee"])
        if f < 0:
            return "L_FEE_INVALID", "费用不能为负"
    except ValueError:
        return "L_FEE_INVALID", "费用不是有效数字"
    return None


def import_batch(conn, filename, data):
    """解析并落库，返回 batch_id 与统计信息。

    - 本地校验失败的行标记 invalid，永不发送；
    - 同一文件内处方号重复的，除首行外标记 L_DUP_RX，永不发送。
    """
    import json
    records, err = parse_csv(data)
    if err:
        return None, {"error": err}

    cur = conn.execute(
        "INSERT INTO batches(filename, total_rows, status, created_at) VALUES(?,0,'pending',?)",
        (filename, db.now_ts()),
    )
    batch_id = cur.lastrowid

    seen = {}
    counts = {"pending": 0, "invalid": 0}
    invalid_rows = []
    for i, rec in enumerate(records, start=2):  # 第1行是表头
        raw_json = json.dumps(rec.get("_extra", []), ensure_ascii=False)
        code_msg = _validate_row(rec)
        rx = rec["rx_no"]
        if code_msg is None and rx in seen:
            code_msg = ("L_DUP_RX", f"文件内处方号重复(首次出现在第 {seen[rx]} 行)")
        if code_msg is None:
            seen.setdefault(rx, i)

        if code_msg:
            code, msg = code_msg
            status, counts_delta = "invalid", "invalid"
            err_code, err_msg = code, msg
            invalid_rows.append({"row_no": i, "rx_no": rx, "error_code": code, "error_message": msg})
        else:
            status, counts_delta = "pending", "pending"
            err_code = err_msg = None

        conn.execute(
            """INSERT INTO prescriptions
               (batch_id,row_no,rx_no,patient_id,drug,quantity,fee,raw,
                status,error_code,error_message)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, i, rx or None, rec["patient_id"], rec["drug"],
             rec["quantity"], rec["fee"], raw_json, status, err_code, err_msg),
        )
        counts[counts_delta] += 1

    conn.execute("UPDATE batches SET total_rows=? WHERE id=?", (len(records), batch_id))
    _recompute_batch_status(conn, batch_id)
    db.log_audit(conn, "import", batch_id=batch_id,
                 detail={"filename": filename, **counts})
    return batch_id, {"batch_id": batch_id, "filename": filename,
                      "total": len(records), **counts, "invalid_rows": invalid_rows}


def _recompute_batch_status(conn, batch_id):
    """根据行状态汇总批次状态。

    pending                 : 仍有待发送
    completed_with_errors   : 无待发送但存在失败/本地无效行
    completed               : 全部成功
    """
    r = conn.execute(
        """SELECT
             SUM(status='pending')  AS p,
             SUM(status='failed')   AS f,
             SUM(status='invalid')  AS iv,
             SUM(status='success')  AS s
           FROM prescriptions WHERE batch_id=?""",
        (batch_id,),
    ).fetchone()
    if r["p"]:
        st = "pending"
    elif r["f"] or r["iv"]:
        st = "completed_with_errors"
    else:
        st = "completed"
    conn.execute("UPDATE batches SET status=? WHERE id=?", (st, batch_id))
    return st
