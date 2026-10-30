"""流水线：解析结果 → 净额 → 映射 → payload → 推送 Cashew。

从零口径：
  - 商户身份与交易名都取解析器的 `display_name`
  - 分类由 MappingStore 决定，无规则时落兜底「其他」
  - 未抵消的退款/抵扣不静默丢弃，记入审计并计数
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .engine import MappedTransaction, plan_pushes, push_batches, remote_alive
from .mapping import MappingStore
from .netting import NettingResult, net_balance
from .parser import Statement
from .records import ImportTransaction
from .timestamps import TimestampSequence

BATCH_SIZE = 200


@dataclass(slots=True)
class PipelineResult:
    net_rows: int = 0
    unmatched: int = 0
    unmatched_total: Decimal = Decimal(0)
    planned: int = 0
    pushed: int = 0
    conflicts: int = 0


def build_items(
    statements: list[Statement],
    mapping: MappingStore,
) -> tuple[list[MappedTransaction], NettingResult]:
    """把解析结果压成待推送交易：净额后剩余额大于 0 的消费。"""
    records = [record for statement in statements for record in statement.records]
    netted = net_balance(records)

    items: list[MappedTransaction] = []
    for spend in netted.spends:
        record = spend.record
        decision = mapping.resolve(record.description, record.display_name)
        txn = ImportTransaction(
            source_record_id=record.record_id,
            date=record.txn_date,
            merchant=record.display_name,
            category="",
            amount=-spend.remaining,  # 账单里消费为正，Cashew 要负数=支出
            gross=spend.gross,
            offset=spend.offset_total,
            note="",
            description=record.description,
        )
        items.append(
            MappedTransaction(
                txn=txn,
                category_pk=decision.category_fk,
                name=decision.name,
            )
        )
    return items, netted


def run_pipeline(
    client,
    statements: list[Statement],
    mapping: MappingStore,
    *,
    exclude: set[str] | None = None,
    seq: TimestampSequence | None = None,
    batch_size: int = BATCH_SIZE,
) -> PipelineResult:
    """对齐远端后按批推送，返回统计。调用方负责 client 的登录态。"""
    items, netted = build_items(statements, mapping)
    remote = remote_alive(client.pull_all()[0])
    plan = plan_pushes(items, remote, exclude=exclude or set(), seq=seq or TimestampSequence())

    result = PipelineResult(
        net_rows=len(items),
        unmatched=len(netted.discarded),
        unmatched_total=-sum((Decimal(r.amount) for r in netted.discarded), Decimal(0)),
        planned=len(plan),
    )
    for batch in push_batches(plan, batch_size):
        response = client.push(batch)
        result.pushed += len(batch)
        result.conflicts += int(response.get("conflictCount", 0))
    return result
