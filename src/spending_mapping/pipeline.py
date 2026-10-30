"""导入：解析 → 净额 → 清洗名字 → 去重 → 推送 Cashew。

新模型下导入**不做分类决策**：名字取 `display_name`，分类一律落兜底「其他」，
审核是独立的一步（见 review.py）。去重双保险：入账台账 + 远端已有 pk。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .categories import load_fallback_pk
from .engine import MappedTransaction, plan_pushes, push_batches, remote_alive
from .ledger import ImportLedger
from .netting import NettingResult, net_balance
from .parser import Statement
from .records import ImportTransaction
from .timestamps import TimestampSequence

BATCH_SIZE = 200


@dataclass(slots=True)
class ImportResult:
    net_rows: int = 0
    skipped_by_ledger: int = 0
    skipped_remote: int = 0
    planned: int = 0
    pushed: int = 0
    conflicts: int = 0
    unmatched: int = 0
    unmatched_total: Decimal = Decimal(0)


def build_items(statements: list[Statement]) -> tuple[list[MappedTransaction], NettingResult]:
    """净额后剩余额大于 0 的消费，名字用 display_name，分类用兜底。"""
    records = [record for statement in statements for record in statement.records]
    netted = net_balance(records)
    fallback = load_fallback_pk()

    items: list[MappedTransaction] = []
    for spend in netted.spends:
        record = spend.record
        txn = ImportTransaction(
            source_record_id=record.record_id,
            date=record.txn_date,
            merchant=record.display_name,
            category="",
            amount=-spend.remaining,  # 账单消费为正，Cashew 要负数=支出
            gross=spend.gross,
            offset=spend.offset_total,
            note="",
            description=record.description,
        )
        items.append(
            MappedTransaction(
                txn=txn,
                category_pk=fallback,
                name=record.display_name,
            )
        )
    return items, netted


def import_transactions(
    client,
    statements: list[Statement],
    ledger: ImportLedger,
    *,
    apply: bool = False,
    limit: int = 0,
    batch_size: int = BATCH_SIZE,
    seq: TimestampSequence | None = None,
) -> ImportResult:
    items, netted = build_items(statements)
    result = ImportResult(
        net_rows=len(items),
        unmatched=len(netted.discarded),
        unmatched_total=-sum((Decimal(r.amount) for r in netted.discarded), Decimal(0)),
    )

    pending = [item for item in items if not ledger.is_imported(item.pk)]
    result.skipped_by_ledger = len(items) - len(pending)

    remote = remote_alive(client.pull_all()[0])
    before = len(pending)
    pending = [item for item in pending if item.pk not in remote]
    result.skipped_remote = before - len(pending)

    if limit:
        pending = pending[:limit]

    plan = plan_pushes(pending, remote, exclude=set(), seq=seq or TimestampSequence())
    result.planned = len(plan)
    if not apply or not plan:
        return result

    for batch in push_batches(plan, batch_size):
        response = client.push(batch)
        result.pushed += len(batch)
        result.conflicts += int(response.get("conflictCount", 0))

    for change in plan:
        payload = change["payload"]
        ledger.mark_imported(
            change["pk"],
            amount=str(payload["amount"]),
            name=str(payload["name"]),
        )
    return result
