"""推送时间戳的生成规则。

服务端按 `modifiedAt` 做 last-write-wins：
  - 比已存值新 → 覆盖
  - 比已存值旧 → 记为冲突并丢弃
  - 同毫秒且 payload 不同 → 记为冲突
  - 超前服务器时间 2 分钟 → 被夹到服务器时间

因此写入方必须保证：同批内严格递增，且不超前服务器时间太多。
"""

from __future__ import annotations

import time

# 服务端允许的最大时钟超前（毫秒），超过会被夹紧
MAX_CLOCK_AHEAD_MS = 2 * 60 * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


class TimestampSequence:
    """严格递增的毫秒序列，跨批次延续，不重复。"""

    def __init__(self, start_ms: int | None = None) -> None:
        self._last = int(start_ms) if start_ms is not None else now_ms() - 1

    @property
    def last(self) -> int:
        return self._last

    def next(self) -> int:
        candidate = max(now_ms(), self._last + 1)
        self._last = candidate
        return candidate

    def take(self, count: int) -> list[int]:
        return [self.next() for _ in range(count)]


def clamp_ahead(modified_ms: int, server_time_ms: int) -> int:
    """复现服务端的夹紧行为，用于本地验证与对账。"""
    limit = server_time_ms + MAX_CLOCK_AHEAD_MS
    return min(modified_ms, limit)
