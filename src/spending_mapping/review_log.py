"""审核日志：append-only 记录两类人工决策。

  - 分类：商户名 → 分类 pk（一个商户一条规则，与 Cashew 的 name 对齐）
  - 退款：哪一笔未匹配退款被如何处理（抵消某笔消费 / 转为收入）

    {"ts": "...", "kind": "category", "name": "盒马鲜生", "category_fk": "2", "action": "edited"}
    {"ts": "...", "kind": "refund", "record_id": "src_...", "action": "offset",
     "target_record_id": "src_...", "category_fk": null}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

CATEGORY = "category"
REFUND = "refund"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._categories: dict[str, str] = {}
        self._refunds: dict[str, dict] = {}

    @classmethod
    def load(cls, path: Path) -> "ReviewLog":
        log = cls(path)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    log._apply(json.loads(line))
        return log

    def _apply(self, entry: dict) -> None:
        if entry["kind"] == CATEGORY:
            self._categories[entry["name"]] = entry["category_fk"]
        elif entry["kind"] == REFUND:
            self._refunds[entry["record_id"]] = entry
        else:
            raise ValueError(f"未知的审核条目类型 {entry['kind']!r}")

    def _append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._apply(entry)

    def category_for(self, name: str) -> str | None:
        return self._categories.get(name)

    def refund_action(self, record_id: str) -> dict | None:
        return self._refunds.get(record_id)

    def decided_names(self) -> set[str]:
        return set(self._categories)

    def record_category(self, name: str, category_fk: str, action: str = "edited") -> None:
        if action not in ("approved", "edited"):
            raise ValueError(f"未知动作 {action!r}")
        self._append({
            "ts": _now_iso(),
            "kind": CATEGORY,
            "name": name,
            "category_fk": category_fk,
            "action": action,
        })

    def record_refund(
        self,
        record_id: str,
        action: str,
        *,
        target_record_id: str | None = None,
        category_fk: str | None = None,
    ) -> None:
        if action not in ("offset", "income"):
            raise ValueError(f"未知退款处理动作 {action!r}")
        if action == "offset" and not target_record_id:
            raise ValueError("offset 需要 target_record_id")
        if action == "income" and not category_fk:
            raise ValueError("income 需要 category_fk")
        self._append({
            "ts": _now_iso(),
            "kind": REFUND,
            "record_id": record_id,
            "action": action,
            "target_record_id": target_record_id,
            "category_fk": category_fk,
        })
