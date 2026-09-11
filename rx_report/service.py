"""报送服务：分批提交、逐行回写、失败行重送。

防重复报送的三层保障：
1. 选数层：SUCCESS 行永远不会被选出（candidates 的 SQL 过滤）；
2. 回写层：mark_business_result 带 SQL 护栏，SUCCESS 不可被降级/覆盖；
3. 接口层：监管端按处方号幂等，重复送达返回原回执，不产生重复记录。
"""
from __future__ import annotations

from datetime import datetime

from .models import SubmitSummary, TransportError


def _new_batch_no() -> str:
    return "B" + datetime.now().strftime("%Y%m%d%H%M%S%f")


def _to_payload(rx) -> dict:
    return {
        "rx_no": rx.rx_no,
        "patient_id": rx.patient_id,
        "drug": rx.drug,
        "quantity": rx.quantity,
        "fee": rx.fee,
    }


def submit(repo, client, *, batch_size: int = 100,
           failed_only: bool = False,
           max_transport_aborts: int = 3) -> SubmitSummary:
    """执行一次报送。

    failed_only=False：报送所有 PENDING/FAILED/TRANSPORT_FAILED 行（成功行除外）。
    failed_only=True ：只重送 FAILED/TRANSPORT_FAILED 行——成功行绝不重送。
    """
    summary = SubmitSummary()
    # 本次运行已处理（已送达或传输失败）的行：不再重复选取，
    # 防止业务失败行在同一次运行中被循环重送。
    processed_this_run: set[str] = set()
    transport_aborts = 0

    while True:
        batch = repo.candidates(
            exclude_success=True,
            failed_only=failed_only,
            exclude_rxnums=processed_this_run,
            batch_size=batch_size,
        )
        if not batch:
            break

        batch_no = _new_batch_no()
        rx_nums = [r.rx_no for r in batch]
        payload = [_to_payload(r) for r in batch]
        summary.total += len(batch)
        summary.batches += 1

        try:
            results = client.submit_batch(payload)
        except TransportError as e:
            # 整批未确认受理：不做任何业务判定，原样保留待重送
            repo.mark_transport_failure(rx_nums, batch_no)
            summary.transport_failed += len(batch)
            summary.batches_lost += 1
            processed_this_run.update(rx_nums)
            transport_aborts += 1
            if transport_aborts >= max_transport_aborts:
                break
            continue

        # 整批已确认送达监管端：本轮不再重复选取
        processed_this_run.update(rx_nums)

        # 逐行回写：成功/失败都落到对应行上
        for res in results:
            if res.accepted:
                # 成功（含重试后成功）：清空旧错误，只留回执
                err_code = err_msg = None
                ack = res.ack_code
            else:
                err_code, err_msg = res.error_code, res.error_message
                ack = None
            repo.mark_business_result(
                res.rx_no,
                accepted=res.accepted,
                ack_code=ack,
                error_code=err_code,
                error_message=err_msg,
                batch_no=batch_no,
            )
            if res.accepted:
                summary.success += 1
            else:
                summary.failed += 1

    return summary
