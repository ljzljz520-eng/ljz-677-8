"""领域模型。"""
from __future__ import annotations

from dataclasses import dataclass

# 行状态
PENDING = "PENDING"   # 待报送
SUCCESS = "SUCCESS"   # 报送成功（监管端已受理）
FAILED = "FAILED"     # 业务失败（监管端逐行拒绝，需要人工修正后重送）
TRANSPORT = "TRANSPORT_FAILED"  # 传输失败（整批未送达，可安全重送）

STATUS_LABELS = {
    PENDING: "待报送",
    SUCCESS: "成功",
    FAILED: "失败",
    TRANSPORT: "传输失败",
}


@dataclass
class Prescription:
    rx_no: str
    patient_id: str
    drug: str
    quantity: int
    fee: float
    id: int | None = None
    status: str = PENDING
    ack_code: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    batch_no: str | None = None
    attempts: int = 0


@dataclass
class RowResult:
    """监管接口对单行的处理结果。"""
    rx_no: str
    accepted: bool
    ack_code: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class TransportError(Exception):
    """整批传输失败（HTTP 5xx / 超时 / 断网）。

    监管端没有受理，也没有任何逐行结果，因此不能把行标记成业务失败。
    """


@dataclass
class SubmitSummary:
    total: int = 0
    success: int = 0
    failed: int = 0
    transport_failed: int = 0
    batches: int = 0
    batches_lost: int = 0
