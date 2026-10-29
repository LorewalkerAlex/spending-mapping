"""推送时间戳的生成规则。

服务端按 `modifiedAt` 做 last-write-wins：
  - 比已存值新 → 覆盖
  - 比已存值旧 → 记为冲突并丢弃
  - 同毫秒且 payload 不同 → 记为冲突
  - 超前服务器时间 2 分钟 → 被夹到服务器时间

因此写入方必须保证：同批内严格递增，且不超前服务器时间太多。

**跨轮次复用**：`TimestampSequence` 必须在一个同步周期内复用（不要每轮新建），
否则同一毫秒内新建的序列会产出相同起始时间戳，被服务端判为"同时间戳不同 payload"冲突。
生产里还应以"服务端已有最大 modifiedAt + 1"作为种子起播，而不是本地时钟。
"""

from __future__ import annotations

import time
from collections.abc import Callable

# 服务端允许的最大时钟超前（毫秒），超过会被夹紧
MAX_CLOCK_AHEAD_MS = 2 * 60 * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


class TimestampSequence:
    """严格递增的毫秒序列，跨批次延续，不重复。

    `clock` 可注入，测试用固定时钟保证确定性；生产用默认的 now_ms()。
    生产里应以"服务端已有最大 modifiedAt + 1"作为 start_ms 起播，
    否则远端一旦被删/改出比本地时钟更大的时间戳，序列会追不上。
    """

    def __init__(
        self,
        start_ms: int | None = None,
        *,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._clock = clock
        self._last = int(start_ms) if start_ms is not None else self._clock() - 1

    @property
    def last(self) -> int:
        return self._last

    def next(self) -> int:
        candidate = max(self._clock(), self._last + 1)
        self._last = candidate
        return candidate

    def take(self, count: int) -> list[int]:
        return [self.next() for _ in range(count)]


def clamp_ahead(modified_ms: int, server_time_ms: int) -> int:
    """复现服务端的夹紧行为，用于本地验证与对账。"""
    limit = server_time_ms + MAX_CLOCK_AHEAD_MS
    return min(modified_ms, limit)
