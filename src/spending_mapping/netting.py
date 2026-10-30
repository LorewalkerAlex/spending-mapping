"""退款/消费金抵扣的净额计算。

规则（沿用 2026-10-27 确认并验证过的口径）：
  - 消费按「商户」分组，组内按交易日倒序（稳定排序，同日保持 record_id 升序）
  - 退款、消费金抵扣按账单顺序逐笔处理，在同商户消费里从最近一笔往前找
    第一条「日期不晚于它、且剩余额足够」的消费扣减
  - 不跨笔分摊：找不到可整笔覆盖的消费，就记未抵消（丢弃并留审计）
  - 剩余额归零的消费不产生净额交易

商户键默认用解析器的 `display_name`（已去渠道前缀与法律后缀）——从零起步不继承
旧 merchants.yaml，名字归一即商户键。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from .parser import Record, display_name


def merchant_key(record: Record) -> str:
    return display_name(record.description)


@dataclass(slots=True)
class NetSpend:
    record: Record
    gross: Decimal
    offset_total: Decimal
    remaining: Decimal


@dataclass(frozen=True, slots=True)
class Offset:
    kind: str
    neg_record_id: str
    neg_date: str
    neg_amount: Decimal
    merchant: str
    spend_record_id: str
    spend_date: str
    spend_gross: Decimal
    spend_remaining: Decimal


@dataclass(slots=True)
class NettingResult:
    spends: list[NetSpend]
    offsets: list[Offset]
    discarded: list[Record]

    @property
    def net_total(self) -> Decimal:
        return sum((item.remaining for item in self.spends), Decimal(0))


def net_balance(records: list[Record]) -> NettingResult:
    ordered = sorted(records, key=lambda r: (r.txn_date, r.record_id))

    spends = [NetSpend(r, Decimal(r.amount), Decimal(0), Decimal(r.amount))
              for r in ordered if r.section == "消费"]

    by_merchant: dict[str, list[NetSpend]] = defaultdict(list)
    for item in spends:
        by_merchant[merchant_key(item.record)].append(item)
    for pool in by_merchant.values():
        pool.sort(key=lambda item: item.record.txn_date, reverse=True)  # 稳定排序

    offsets: list[Offset] = []
    discarded: list[Record] = []

    def apply(negative: Record, amount: Decimal, kind: str) -> bool:
        key = merchant_key(negative)
        for item in by_merchant.get(key, []):
            if item.record.txn_date > negative.txn_date:
                continue
            if item.remaining >= amount:
                item.remaining -= amount
                item.offset_total += amount
                offsets.append(Offset(
                    kind=kind,
                    neg_record_id=negative.record_id,
                    neg_date=negative.txn_date,
                    neg_amount=-amount,
                    merchant=key,
                    spend_record_id=item.record.record_id,
                    spend_date=item.record.txn_date,
                    spend_gross=item.gross,
                    spend_remaining=item.remaining,
                ))
                return True
        return False

    for record in ordered:
        if record.section == "退款":
            if not apply(record, -Decimal(record.amount), "退款"):
                discarded.append(record)
    for record in ordered:
        if record.section == "其他":
            if not apply(record, -Decimal(record.amount), "消费金抵扣"):
                discarded.append(record)

    return NettingResult(
        spends=[item for item in spends if item.remaining > 0],
        offsets=offsets,
        discarded=discarded,
    )
