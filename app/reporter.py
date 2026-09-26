"""报送引擎：分批提交、结果逐行回写、只重送失败行。

防重复设计:
  1. 行级状态机 pending/failed/success/invalid；
     submit 只取 pending，retry_failed 只取 failed；
     任何接口都不会再次提交 success / invalid 行。
  2. 批次级进程锁 + DB 状态锁，并发第二次提交返回 409/blocked，杜绝双击整批重传。
  3. 监管端以处方号为幂等键，已受理的处方号再次提交返回 4091 DUPLICATE_RX。
"""
import threading

from . import config, db
from .importer import _recompute_batch_status
from . import regulator_client

# 批次级互斥锁（单进程多线程足够；多实例部署时应换 DB/分布式锁）
_batch_locks = {}
_locks_guard = threading.Lock()


def _lock_for(batch_id):
    with _locks_guard:
        lk = _batch_locks.get(batch_id)
        if lk is None:
            lk = threading.Lock()
            _batch_locks[batch_id] = lk
        return lk


# 可重试错误码（瞬时类）。监管端返回的其它错误均视为永久性业务拒绝。
RETRIABLE_CODES = {"E_RETRY", "E_TIMEOUT", "E_NO_RESULT"}


def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _rows_to_payload(rows):
    return [{
        "rx_no": r["rx_no"],
        "patient_id": r["patient_id"],
        "drug": r["drug"],
        "quantity": r["quantity"],
        "fee": r["fee"],
    } for r in rows]


def _mark_sending(conn, rows, event):
    """发送前登记尝试次数。"""
    ts = db.now_ts()
    for r in rows:
        conn.execute(
            "UPDATE prescriptions SET attempts=attempts+1, last_sent_at=? WHERE id=?",
            (ts, r["id"]),
        )
        db.log_audit(conn, event, batch_id=r["batch_id"], rx_no=r["rx_no"])


def _apply_results(conn, rows, results, chunk_idx):
    """把监管端逐行结果回写到每一行。返回 (success, failed) 计数。"""
    by_rx = {r["rx_no"]: r for r in results}
    ts = db.now_ts()
    ok = fail = 0
    for r in rows:
        res = by_rx.get(r["rx_no"])
        if res is None:
            res = {"accepted": False, "regulator_id": None,
                   "error_code": "E_NO_RESULT", "error_message": "监管端未返回该行结果",
                   "retriable": True}
        if res.get("accepted"):
            conn.execute(
                """UPDATE prescriptions SET status='success',
                   regulator_id=?, error_code=NULL, error_message=NULL, last_result_at=?
                   WHERE id=?""",
                (res.get("regulator_id"), ts, r["id"]),
            )
            db.log_audit(conn, "success", batch_id=r["batch_id"], rx_no=r["rx_no"],
                         detail={"regulator_id": res.get("regulator_id"), "chunk": chunk_idx})
            ok += 1
        else:
            conn.execute(
                """UPDATE prescriptions SET status='failed',
                   error_code=?, error_message=?, last_result_at=? WHERE id=?""",
                (res.get("error_code"), res.get("error_message"), ts, r["id"]),
            )
            db.log_audit(conn, "failed", batch_id=r["batch_id"], rx_no=r["rx_no"],
                         detail={"error_code": res.get("error_code"),
                                 "error_message": res.get("error_message"), "chunk": chunk_idx})
            fail += 1
    return ok, fail


def _apply_transport_error(conn, rows, exc, chunk_idx):
    """chunk 传输级失败：本 chunk 全部行标 failed + 可重试错误码。"""
    ts = db.now_ts()
    for r in rows:
        conn.execute(
            """UPDATE prescriptions SET status='failed',
               error_code='E_RETRY', error_message=?, last_result_at=? WHERE id=?""",
            (f"批次级传输失败(第{chunk_idx}片): {exc}", ts, r["id"]),
        )
        db.log_audit(conn, "failed", batch_id=r["batch_id"], rx_no=r["rx_no"],
                     detail={"error_code": "E_RETRY", "chunk": chunk_idx, "transport": str(exc)})


def _send(conn, batch_id, rows, event, client, chunk_size):
    """把给定行集分 chunk 发送。success/invalid 永远不会出现在 rows 中。"""
    sent_ok = sent_fail = chunks_ok = chunks_failed = 0
    for idx, chunk in enumerate(_chunk(rows, chunk_size), start=1):
        payload = _rows_to_payload(chunk)
        _mark_sending(conn, chunk, event)
        try:
            results = client(payload, batch_ref=f"batch-{batch_id}")
        except regulator_client.TransportError as exc:
            _apply_transport_error(conn, chunk, exc, idx)
            sent_fail += len(chunk)
            chunks_failed += 1
            continue
        ok, fail = _apply_results(conn, chunk, results, idx)
        sent_ok += ok
        sent_fail += fail
        chunks_ok += 1
    _recompute_batch_status(conn, batch_id)
    return {"rows_sent": len(rows), "success": sent_ok, "failed": sent_fail,
            "chunks_ok": chunks_ok, "chunks_failed": chunks_failed}


def _is_busy(conn, batch_id):
    """DB 状态锁：只要批次里存在 pending 或 failed 行，就认为该批次可执行；
    真正的并发互斥由进程锁保证。这里返回批次当前是否被标记为处理中。"""
    r = conn.execute("SELECT status FROM batches WHERE id=?", (batch_id,)).fetchone()
    return r is not None and r["status"] == "processing"


def run(batch_id, mode="submit", client=None, chunk_size=None,
        force_flaky=False, force_outage=None, delay_ms=0):
    """执行报送。

    mode='submit'       : 仅发送 pending 行（首次报送）
    mode='retry_failed' : 仅发送 failed 行（重送失败行）
    返回 dict；并发冲突返回 {'blocked': True, ...}。
    """
    client = client or (lambda records, batch_ref: regulator_client.post_prescriptions(
        records, batch_ref, force_flaky=force_flaky,
        force_outage=force_outage, delay_ms=delay_ms))
    chunk_size = chunk_size or config.CHUNK_SIZE
    lock = _lock_for(batch_id)
    if not lock.acquire(blocking=False):
        with db.get_conn() as conn:
            db.log_audit(conn, "blocked", batch_id=batch_id,
                         detail=f"并发{mode}请求被拒绝，防止重复提交")
        return {"blocked": True, "reason": "该批次正在报送中，请勿重复提交", "mode": mode}

    try:
        with db.get_conn() as conn:
            batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                return {"error": "批次不存在", "status": 404}
            if _is_busy(conn, batch_id):
                return {"blocked": True, "reason": "批次处于处理中状态", "mode": mode}

            if mode == "retry_failed":
                want = "failed"
                event = "retry"
            else:
                want = "pending"
                event = "submit"

            rows = conn.execute(
                "SELECT * FROM prescriptions WHERE batch_id=? AND status=? ORDER BY row_no",
                (batch_id, want),
            ).fetchall()
            if not rows:
                # 关键保护：没有可发送行时绝不重传，明确告知不会重复报送
                if mode == "retry_failed":
                    return {"blocked": False, "rows_sent": 0,
                            "reason": "没有失败行需要重送（成功行不会被重复报送）",
                            "mode": mode}
                return {"blocked": False, "rows_sent": 0,
                        "reason": "没有待报送行（可能已报送或重送过）", "mode": mode}

            conn.execute("UPDATE batches SET status='processing', submitted_at=COALESCE(submitted_at,?) WHERE id=?",
                         (db.now_ts(), batch_id))
            db.log_audit(conn, event, batch_id=batch_id,
                         detail={"rows": len(rows), "chunk_size": chunk_size})

        # HTTP 调用放在事务外，避免长时间占用写锁
        with db.get_conn() as conn:
            summary = _send(conn, batch_id, rows, event, client, chunk_size)

        with db.get_conn() as conn:
            bstatus = _recompute_batch_status(conn, batch_id)
            db.log_audit(conn, mode, batch_id=batch_id, detail=summary)

        summary.update({"blocked": False, "mode": mode, "batch_status": bstatus})
        return summary
    finally:
        lock.release()
