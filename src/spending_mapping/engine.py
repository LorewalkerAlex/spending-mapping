"""同步引擎：计算"应然"与"实然"的差集，生成待推变更。

不碰网络。输入应然集合（我们从账单推导出的交易）与实然集合（服务端 pull 下来的行），
输出要推送的 change 列表。规则见 docs/PIPELINE-DESIGN.md §6.3 / §7：

  - 远端不存在        → 推（新增）
  - 远端内容与推导一致 → 跳过
  - 远端被人工改过（在排除集内）→ 跳过，保留人工结果
  - 远端内容不一致且不在排除集 → 推（整行覆盖，新时间戳）
  - 远端被删（tombstone）      → 推（补回）

内容相等用 `payload_digest`（已排除 dateTimeModified），所以"只是时间戳变了"不算变化。
"""

from __future__ import annotations

from dataclasses import dataclass

from .cashew_payload import build_change, build_payload, payload_digest
from .records import ImportTransaction
from .timestamps import TimestampSequence

BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class MappedTransaction:
    """一笔"应然"交易：导入集记录 + 已解析的分类/钱包/交易名。"""

    txn: ImportTransaction
    category_pk: str
    wallet_pk: str = "0"
    income: bool = False
    name: str | None = None

    @property
    def pk(self) -> str:
        return self.txn.source_record_id


@dataclass(slots=True)
class SyncResult:
    planned: int = 0
    pushed: int = 0
    conflicts: int = 0
    rounds: int = 0


def remote_alive(changes: list[dict]) -> dict[str, dict]:
    """把服务端 pull 结果折叠成 `pk → 最新存活行`，只关心 Transaction。"""
    alive: dict[str, dict] = {}
    for change in changes:
        if change.get("table") != "Transaction":
            continue
        pk = change["pk"]
        if change.get("deleted"):
            alive.pop(pk, None)
        else:
            alive[pk] = change
    return alive


def _payload_for(item: MappedTransaction, modified_ms: int) -> dict:
    return build_payload(
        item.txn,
        category_pk=item.category_pk,
        wallet_pk=item.wallet_pk,
        modified_ms=modified_ms,
        income=item.income,
        name=item.name,
    )


def plan_pushes(
    items: list[MappedTransaction],
    remote: dict[str, dict],
    *,
    exclude: set[str],
    seq: TimestampSequence,
) -> list[dict]:
    """返回需要推送的 change 列表，用 `seq` 生成严格递增的新时间戳。"""
    plan: list[dict] = []
    for item in items:
        if item.pk in exclude:
            continue
        current = remote.get(item.pk)
        if current is None:
            ts = seq.next()
            plan.append(build_change(_payload_for(item, ts), ts))
            continue
        # 内容比较不含 dateTimeModified；用 0 当占位时间戳做比较即可
        if payload_digest(_payload_for(item, 0)) != payload_digest(current.get("payload") or {}):
            ts = seq.next()
            plan.append(build_change(_payload_for(item, ts), ts))
    return plan


def push_batches(changes: list[dict], batch_size: int = BATCH_SIZE) -> list[list[dict]]:
    return [changes[i : i + batch_size] for i in range(0, len(changes), batch_size)]


def sync_once(
    server,
    items: list[MappedTransaction],
    *,
    exclude: set[str],
    seq: TimestampSequence,
    batch_size: int = BATCH_SIZE,
) -> SyncResult:
    remote = remote_alive(server.pull_all())
    plan = plan_pushes(items, remote, exclude=exclude, seq=seq)
    result = SyncResult(planned=len(plan))
    for batch in push_batches(plan, batch_size):
        response = server.push(batch)
        result.pushed += len(batch)
        result.conflicts += int(response.get("conflictCount", 0))
    result.rounds = 1
    return result


def sync_until_clean(
    server,
    items: list[MappedTransaction],
    *,
    exclude: set[str],
    seq: TimestampSequence,
    batch_size: int = BATCH_SIZE,
    max_rounds: int = 5,
) -> SyncResult:
    """反复同步直到没有待推（幂等重推会收敛）；用于部分失败/断线重试。"""
    total = SyncResult()
    for _ in range(max_rounds):
        result = sync_once(server, items, exclude=exclude, seq=seq, batch_size=batch_size)
        total.planned += result.planned
        total.pushed += result.pushed
        total.conflicts += result.conflicts
        total.rounds += 1
        if result.planned == 0:
            break
    return total
