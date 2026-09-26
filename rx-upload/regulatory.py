# -*- coding: utf-8 -*-
"""
模拟监管接口（外部系统）。
真实部署时把 submit_batch 换成对监管平台 HTTP API 的调用即可，
入参/出参结构保持不变。

监管侧自己也维护一本"已受理台账"(regulatory_ledger)，
用幂等键去重：同一幂等键重复报送不会重复入账。
"""
import sqlite3

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS regulatory_ledger (
    idempotency_key TEXT PRIMARY KEY,
    rx_no TEXT NOT NULL,
    accepted_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def ensure_ledger(conn: sqlite3.Connection):
    conn.execute(LEDGER_DDL)


def submit_batch(conn: sqlite3.Connection, items):
    """
    items: [{idempotency_key, rx_no, patient_id, drug, quantity, fee, attempts}]
    返回: {idempotency_key: {"ok": bool, "code": str, "message": str}}
    """
    ensure_ledger(conn)
    results = {}
    for it in items:
        key = it["idempotency_key"]

        # ---- 监管侧幂等：已受理过的键直接返回成功，不重复入账 ----
        dup = conn.execute(
            "SELECT rx_no FROM regulatory_ledger WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if dup:
            results[key] = {
                "ok": True,
                "code": "DUPLICATE_IGNORED",
                "message": "监管侧已受理，幂等去重（未重复入账）",
            }
            continue

        # ---- 业务校验（永久性错误，重送多少次都不会成功）----
        if not it["patient_id"]:
            results[key] = {"ok": False, "code": "E_PATIENT", "message": "患者编号缺失"}
            continue
        if it["quantity"] <= 0:
            results[key] = {"ok": False, "code": "E_QTY", "message": "数量必须大于 0"}
            continue
        if it["fee"] < 0:
            results[key] = {"ok": False, "code": "E_FEE", "message": "费用不能为负数"}
            continue

        # ---- 模拟瞬时故障：冷链药品首次报送超时，重试即可成功 ----
        if "冷链" in it["drug"] and it["attempts"] == 0:
            results[key] = {
                "ok": False,
                "code": "E_TRANSIENT",
                "message": "冷链通道超时（模拟瞬时故障），请重试",
            }
            continue

        # ---- 受理并入监管台账 ----
        conn.execute(
            "INSERT INTO regulatory_ledger (idempotency_key, rx_no) VALUES (?, ?)",
            (key, it["rx_no"]),
        )
        results[key] = {"ok": True, "code": "OK", "message": "监管平台已受理"}
    return results
