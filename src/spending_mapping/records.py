"""导入集的数据模型。

数据来源是净额计算产物（`net-transactions.json`），不含邮件原件；
每笔的 `source_record_id` 由邮件字节与行定位决定，重算不改变。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ImportTransaction:
    source_record_id: str
    date: str  # YYYY-MM-DD
    merchant: str
    category: str
    amount: Decimal  # 净额，负数为支出
    gross: Decimal
    offset: Decimal
    note: str
    description: str

    @classmethod
    def from_dict(cls, raw: dict) -> "ImportTransaction":
        return cls(
            source_record_id=raw["source_record_id"],
            date=raw["date"],
            merchant=raw["merchant"],
            category=raw["category"],
            amount=Decimal(raw["amount"]),
            gross=Decimal(raw["gross"]),
            offset=Decimal(raw["offset"]),
            note=raw.get("note", ""),
            description=raw.get("description", ""),
        )


def load_transactions(path: Path) -> list[ImportTransaction]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload["consumption"] if isinstance(payload, dict) else payload
    return [ImportTransaction.from_dict(row) for row in rows]
