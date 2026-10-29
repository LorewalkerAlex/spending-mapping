"""内存版假服务端：复现 Cashew `/sync/push` 与 `/sync/pull` 的文档语义。

只用于本地测试，不对外。语义来源 docs/server/sync.md：

  - sync_records 每行一条、只存最新版本
  - LWW：无行 insert；更新覆盖；同时间戳同 payload no-op；同时间戳异 payload conflict；
    更旧 conflict 丢弃
  - modifiedAt 超前 server_time 2 分钟被夹到 server_time + 2min
  - pull?since 低于 min_retained_seq → 409 rebootstrap
  - push 返回 {serverTime, conflictCount}；pull 按 seq 升序、带 nextCursor/hasMore
"""

from __future__ import annotations

from dataclasses import dataclass, field


class RebootstrapError(Exception):
    def __init__(self, min_retained_seq: int) -> None:
        super().__init__(f"rebootstrap: since below min_retained_seq {min_retained_seq}")
        self.min_retained_seq = min_retained_seq


CLAMP_MS = 2 * 60 * 1000


@dataclass
class FakeCashewServer:
    server_time_ms: int = 1_800_000_000_000
    batch_size: int = 500
    _records: dict[tuple[str, str], dict] = field(default_factory=dict)
    _next_seq: int = field(default=1, init=False)
    _min_retained_seq: int = field(default=0, init=False)
    applied: int = field(default=0, init=False)
    conflicts: int = field(default=0, init=False)
    fail_next_push: int = field(default=0, init=False)  # >0 时下一次 push 抛异常

    def _clamp(self, modified_at: int) -> int:
        return min(modified_at, self.server_time_ms + CLAMP_MS)

    def push(self, changes: list[dict]) -> dict:
        if self.fail_next_push > 0:
            self.fail_next_push -= 1
            raise RuntimeError("simulated push failure")
        applied = 0
        conflicts = 0
        for change in changes:
            key = (change["table"], change["pk"])
            modified_at = self._clamp(change["modifiedAt"])
            current = self._records.get(key)
            if current is None:
                self._records[key] = dict(change, modifiedAt=modified_at, seq=self._next_seq)
                self._next_seq += 1
                self.applied += 1
                applied += 1
            elif modified_at > current["modifiedAt"]:
                self._records[key] = dict(change, modifiedAt=modified_at, seq=self._next_seq)
                self._next_seq += 1
                self.applied += 1
                applied += 1
            elif modified_at == current["modifiedAt"]:
                if change.get("payload") != current.get("payload") or change.get("deleted") != current.get("deleted"):
                    self.conflicts += 1
                    conflicts += 1
                # 同时间戳同 payload → no-op
            else:
                self.conflicts += 1
                conflicts += 1
        return {"serverTime": self.server_time_ms, "conflictCount": conflicts}

    def pull(self, since: int = 0, limit: int | None = None) -> dict:
        limit = limit or self.batch_size
        if since < self._min_retained_seq:
            raise RebootstrapError(self._min_retained_seq)
        rows = [record for record in self._records.values() if record["seq"] > since]
        rows.sort(key=lambda r: r["seq"])
        page = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = page[-1]["seq"] if page else since
        return {
            "changes": page,
            "nextCursor": next_cursor,
            "hasMore": has_more,
            "serverTime": self.server_time_ms,
        }

    def pull_all(self) -> list[dict]:
        collected: list[dict] = []
        cursor = 0
        while True:
            page = self.pull(cursor, limit=2000)
            collected.extend(page["changes"])
            if not page["hasMore"]:
                break
            cursor = page["nextCursor"]
        return collected

    def reset(self) -> dict:
        self._records.clear()
        self._min_retained_seq = self._next_seq
        self._next_seq += 1  # 烧一个序号做 barrier
        return {"minRetainedSeq": self._min_retained_seq}

    def alive(self, table: str = "Transaction") -> dict[str, dict]:
        return {
            pk: record
            for (t, pk), record in self._records.items()
            if t == table and not record.get("deleted")
        }
