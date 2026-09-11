"""监管接口客户端（Mock 实现，接口形态与真实 HTTP 接口一致）。

关键行为：
1. 分批提交：submit_batch(payload) -> list[RowResult]
2. 整批传输失败抛 TransportError（如 503/超时），此时没有任何逐行结果；
3. 服务端幂等：以处方号为幂等键，重复提交的已受理行返回原回执（DUPLICATE_ACK），
   绝不产生重复监管记录。
"""
from __future__ import annotations

import random
import zlib
from dataclasses import dataclass

from .models import RowResult, TransportError

# 业务错误码
E_PATIENT_FORMAT = "E_PATIENT_ID"
E_DRUG_UNKNOWN = "E_DRUG_UNKNOWN"
E_QTY_INVALID = "E_QTY_INVALID"
E_FEE_LIMIT = "E_FEE_LIMIT"
E_FIELD_MISSING = "E_FIELD_MISSING"

# 监管端维护的“药品目录”
KNOWN_DRUGS = {
    "阿莫西林胶囊", "头孢克肟片", "布洛芬缓释胶囊", "二甲双胍片",
    "氨氯地平片", "奥美拉唑肠溶胶囊", "氯雷他定片",
}

FEE_LIMIT = 10000.0  # 单张门诊处方费用上限


@dataclass
class RegulatorClient:
    """内存版监管端：维护已受理集合，模拟网络故障。

    flaky_rate   : 每批按此概率发生一次“整批传输失败”
    flaky_rxnums : 这些处方第一次随批到达时先制造一次传输失败（可复现的抖动），
                   之后恢复正常——用于演示“瞬时失败 -> 重送成功”。
    """
    flaky_rate: float = 0.0
    flaky_rxnums: tuple[str, ...] = ()
    seed: int | None = None

    def __post_init__(self):
        self._accepted: dict[str, str] = {}   # rx_no -> ack
        self._delivered_once: set[str] = set()
        self.delivered_batches: list[dict] = []
        self._rng = random.Random(self.seed)

    # -- 真实接口在这里会是 HTTP POST；Mock 直接走本地校验 -----------------------
    def submit_batch(self, batch: list[dict]) -> list[RowResult]:
        rx_nums = [r["rx_no"] for r in batch]

        # 1) 可复现的“单行所在批次”瞬时传输故障（第一次经过时）
        shaky = [
            n for n in rx_nums
            if n in self.flaky_rxnums and n not in self._delivered_once
        ]
        # 2) 按概率发生的整批传输故障
        lottery = self._rng.random() < self.flaky_rate

        if shaky or lottery:
            for n in rx_nums:
                self._delivered_once.add(n)
            raise TransportError(
                f"监管接口不可用（503/超时），整批 {len(batch)} 行未确认受理")

        for n in rx_nums:
            self._delivered_once.add(n)
        # 记录真正到达监管端的批次，供幂等性测试核对
        self.delivered_batches.append(
            {"rx_nums": list(rx_nums), "size": len(batch)})

        return [self._process_row(r) for r in batch]

    # -- 逐行业务校验（真实系统对应监管端的规则引擎） ---------------------------
    def _process_row(self, r: dict) -> RowResult:
        rx_no = r["rx_no"]

        # 服务端幂等：已受理的处方直接返回原回执，绝不重复入库
        if rx_no in self._accepted:
            return RowResult(
                rx_no=rx_no, accepted=True,
                ack_code=self._accepted[rx_no],
                error_code="DUPLICATE_ACK",
                error_message="该处方已受理，返回原回执（幂等）")

        code, msg = self._validate(r)
        if code is not None:
            return RowResult(rx_no, False, error_code=code, error_message=msg)

        ack = f"ACK-{r['rx_no']}-{zlib.crc32(rx_no.encode()) & 0xffffff:06x}"
        self._accepted[rx_no] = ack
        return RowResult(rx_no, True, ack_code=ack)

    @staticmethod
    def _validate(r: dict) -> tuple[str | None, str | None]:
        for field in ("rx_no", "patient_id", "drug", "quantity", "fee"):
            if r.get(field) in (None, ""):
                return E_FIELD_MISSING, f"字段 {field} 为空"

        pid = str(r["patient_id"]).strip()
        if not (pid.startswith("P") and pid[1:].isdigit() and len(pid) >= 5):
            return E_PATIENT_FORMAT, f"患者编号 '{pid}' 格式应为 P+数字"

        if str(r["drug"]).strip() not in KNOWN_DRUGS:
            return E_DRUG_UNKNOWN, f"药品 '{r['drug']}' 不在监管目录"

        qty = r["quantity"]
        if not isinstance(qty, int) or qty <= 0:
            return E_QTY_INVALID, f"数量 {qty} 必须为正整数"

        if float(r["fee"]) > FEE_LIMIT:
            return E_FEE_LIMIT, f"费用 {r['fee']} 超过单张处方上限 {FEE_LIMIT:.0f}"

        return None, None
