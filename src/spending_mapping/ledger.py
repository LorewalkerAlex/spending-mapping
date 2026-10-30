"""入账台账：本地记录哪些原始交易已经导入过 Cashew。

为什么需要：Cashew 会被 reset/清空重建，但"哪些邮件记录已经入过账"是我们的账本事实，
不能跟着 Cashew 一起丢。台账用 append-only 事件流，当前状态 = 每条记录的最后一条事件。

    {"ts": "...", "event": "imported", "record_id": "src_...", "amount": "-297.00", "name": "..."}
    {"ts": "...", "event": "deleted",  "record_id": "src_..."}

去重：导入前看台账（已 imported 且未 deleted 跳过），再看远端（已有同 pk 跳过）——双重保险。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

IMPORTED = "imported"
DELETED = "deleted"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    record_id: str
    state: str  # imported | deleted
    amount: str | None = None
    name: str | None = None


class ImportLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._state: dict[str, LedgerEntry] = {}

    @classmethod
    def load(cls, path: Path) -> "ImportLedger":
        ledger = cls(path)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    ledger._apply(json.loads(line))
        return ledger

    def _apply(self, event: dict) -> None:
        record_id = event["record_id"]
        if event["event"] == IMPORTED:
            self._state[record_id] = LedgerEntry(
                record_id=record_id,
                state=IMPORTED,
                amount=event.get("amount"),
                name=event.get("name"),
            )
        elif event["event"] == DELETED:
            self._state[record_id] = LedgerEntry(record_id=record_id, state=DELETED)
        else:
            raise ValueError(f"未知的台账事件 {event['event']!r}")

    def _append(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._apply(event)

    def mark_imported(self, record_id: str, *, amount: str, name: str) -> None:
        self._append({
            "ts": _now_iso(),
            "event": IMPORTED,
            "record_id": record_id,
            "amount": amount,
            "name": name,
        })

    def mark_deleted(self, record_id: str) -> None:
        self._append({"ts": _now_iso(), "event": DELETED, "record_id": record_id})

    def is_imported(self, record_id: str) -> bool:
        entry = self._state.get(record_id)
        return entry is not None and entry.state == IMPORTED

    def imported_ids(self) -> set[str]:
        return {rid for rid, entry in self._state.items() if entry.state == IMPORTED}

    def __len__(self) -> int:
        return len(self.imported_ids())
