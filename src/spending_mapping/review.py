"""审核：从 Cashew 拉取现值，按商户名分组做分类审核；未抵消退款做匹配审核。

分类审核的队列成员 = 已入账（`src_` 前缀）但商户名还没有决策的交易。
退款审核的队列成员 = 净额时未抵消、且还没有处理记录的退款/抵扣。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .cashew_payload import build_change, build_payload, payload_digest
from .engine import push_batches, remote_alive
from .netting import net_balance
from .parser import Record, Statement, display_name
from .records import ImportTransaction
from .review_log import ReviewLog
from .timestamps import TimestampSequence

IMPORTED_PREFIX = "src_"
BATCH_SIZE = 200


@dataclass(slots=True)
class CategoryItem:
    name: str
    count: int
    total: Decimal
    record_ids: list[str] = field(default_factory=list)
    sample_note: str = ""


@dataclass(slots=True)
class RefundItem:
    record_id: str
    date: str
    amount: Decimal
    merchant: str
    description: str


def list_pending_categories(client, log: ReviewLog) -> list[CategoryItem]:
    changes, _ = client.pull_all()
    alive = remote_alive(changes)
    decided = log.decided_names()
    groups: dict[str, list[dict]] = defaultdict(list)

    for pk, change in alive.items():
        if not pk.startswith(IMPORTED_PREFIX):
            continue
        payload = change.get("payload") or {}
        name = str(payload.get("name") or "").strip()
        if not name or name in decided:
            continue
        groups[name].append(change)

    items: list[CategoryItem] = []
    for name, rows in groups.items():
        total = sum((Decimal(str((row.get("payload") or {}).get("amount", 0))) for row in rows), Decimal(0))
        sample = str((rows[0].get("payload") or {}).get("note") or "")
        items.append(CategoryItem(
            name=name,
            count=len(rows),
            total=total,
            record_ids=[row["pk"] for row in rows],
            sample_note=sample,
        ))
    items.sort(key=lambda item: item.total)
    return items


def apply_category(
    client,
    name: str,
    category_fk: str,
    log: ReviewLog,
    *,
    apply: bool = True,
    batch_size: int = BATCH_SIZE,
) -> int:
    """把某个商户名下的所有已入账交易改成指定分类，并记日志。"""
    changes, _ = client.pull_all()
    alive = remote_alive(changes)
    targets = [
        change for pk, change in alive.items()
        if pk.startswith(IMPORTED_PREFIX)
        and str((change.get("payload") or {}).get("name") or "").strip() == name
    ]
    if not targets:
        return 0

    seq = TimestampSequence()
    updates = []
    for change in targets:
        payload = dict(change.get("payload") or {})
        if str(payload.get("categoryFk")) == category_fk:
            continue
        payload["categoryFk"] = category_fk
        payload["dateTimeModified"] = seq.next()
        updates.append(build_change(payload, payload["dateTimeModified"]))

    if apply and updates:
        for batch in push_batches(updates, batch_size):
            client.push(batch)
    log.record_category(name, category_fk, action="edited")
    return len(updates)


def list_pending_refunds(statements: list[Statement], log: ReviewLog) -> list[RefundItem]:
    records = [record for statement in statements for record in statement.records]
    netted = net_balance(records)
    items = []
    for record in netted.discarded:
        if log.refund_action(record.record_id) is not None:
            continue
        items.append(RefundItem(
            record_id=record.record_id,
            date=record.txn_date,
            amount=Decimal(record.amount),
            merchant=display_name(record.description),
            description=record.description,
        ))
    return items


def _find_record(statements: list[Statement], record_id: str) -> Record | None:
    for statement in statements:
        for record in statement.records:
            if record.record_id == record_id:
                return record
    return None


def apply_refund_offset(
    client,
    statements: list[Statement],
    refund_record_id: str,
    target_record_id: str,
    log: ReviewLog,
    *,
    apply: bool = True,
) -> bool:
    """把退款抵消到指定消费：目标金额减去退款额，重推。"""
    refund = _find_record(statements, refund_record_id)
    if refund is None:
        raise ValueError(f"找不到退款记录 {refund_record_id}")

    changes, _ = client.pull_all()
    target = remote_alive(changes).get(target_record_id)
    if target is None:
        raise ValueError(f"Cashew 里找不到目标交易 {target_record_id}")

    payload = dict(target.get("payload") or {})
    old_amount = Decimal(str(payload.get("amount", 0)))
    new_amount = old_amount - Decimal(refund.amount)  # 退款为负，减去即降低支出
    if new_amount >= 0:
        raise ValueError(f"抵消后金额非负：{old_amount} - {refund.amount} = {new_amount}")

    payload["amount"] = float(new_amount)
    seq = TimestampSequence()
    payload["dateTimeModified"] = seq.next()

    if apply:
        client.push([build_change(payload, payload["dateTimeModified"])])
    log.record_refund(refund_record_id, "offset", target_record_id=target_record_id)
    return True


def apply_refund_income(
    client,
    statements: list[Statement],
    refund_record_id: str,
    category_fk: str,
    log: ReviewLog,
    *,
    apply: bool = True,
) -> bool:
    """把未匹配退款记为一笔收入。"""
    refund = _find_record(statements, refund_record_id)
    if refund is None:
        raise ValueError(f"找不到退款记录 {refund_record_id}")

    name = display_name(refund.description)
    txn = ImportTransaction(
        source_record_id=refund.record_id,
        date=refund.txn_date,
        merchant=name,
        category="",
        amount=-Decimal(refund.amount),  # 退款为负 → 收入为正
        gross=-Decimal(refund.amount),
        offset=Decimal(0),
        note="未匹配退款",
        description=refund.description,
    )
    seq = TimestampSequence()
    payload = build_payload(
        txn,
        category_pk=category_fk,
        modified_ms=seq.next(),
        income=True,
        name=name,
    )
    if apply:
        client.push([build_change(payload, payload["dateTimeModified"])])
    log.record_refund(refund_record_id, "income", category_fk=category_fk)
    return True
