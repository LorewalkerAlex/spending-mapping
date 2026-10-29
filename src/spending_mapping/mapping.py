"""映射决策：描述 → (交易名, 分类 pk)。

从零起步（用户 2026-10-30 决定）：不继承旧 merchants.yaml / categories.yaml，
名字用解析器的 `display_name`，分类没有规则时落兜底「其他」并进待审队列。

规则存 append-only 的 `review-log.jsonl`，一行一条人工决策：

    {"ts": "...", "description": "...", "name": "...", "category_fk": "...",
     "action": "approved|edited", "note": ""}

`action` 决定来源：approved → human_approved，edited → human_edited；
未命中规则 → auto（机器默认，可被人工覆盖）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .categories import Category, load_fallback_pk

SOURCE_BY_ACTION = {"approved": "human_approved", "edited": "human_edited"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class MappingDecision:
    name: str
    category_fk: str
    source: str  # auto | human_approved | human_edited


class MappingStore:
    """按 description 解析交易名与分类；人工决策写 append-only 日志。"""

    def __init__(
        self,
        categories: list[Category],
        fallback_pk: str,
        log_path: Path,
    ) -> None:
        self._pks = {category.pk for category in categories}
        self._fallback = fallback_pk
        self._log_path = log_path
        self._by_description: dict[str, tuple[str, str, str]] = {}

    @classmethod
    def load(cls, categories: list[Category], log_path: Path) -> "MappingStore":
        store = cls(categories, load_fallback_pk(), log_path)
        store._replay()
        return store

    def _replay(self) -> None:
        if not self._log_path.exists():
            return
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            self._by_description[entry["description"]] = (
                entry["name"],
                entry["category_fk"],
                SOURCE_BY_ACTION[entry["action"]],
            )

    def resolve(self, description: str, display_name: str) -> MappingDecision:
        decision = self._by_description.get(description)
        if decision is not None:
            name, category_fk, source = decision
            return MappingDecision(name=name, category_fk=category_fk, source=source)
        return MappingDecision(name=display_name, category_fk=self._fallback, source="auto")

    def apply(
        self,
        description: str,
        *,
        name: str,
        category_fk: str,
        action: str,
        note: str = "",
    ) -> MappingDecision:
        if action not in SOURCE_BY_ACTION:
            raise ValueError(f"未知的审核动作 {action!r}")
        if category_fk not in self._pks:
            raise ValueError(f"分类 {category_fk!r} 不在已知分类中")
        if not name.strip():
            raise ValueError("交易名不能为空")

        entry = {
            "ts": _now_iso(),
            "description": description,
            "name": name,
            "category_fk": category_fk,
            "action": action,
            "note": note,
        }
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        source = SOURCE_BY_ACTION[action]
        self._by_description[description] = (name, category_fk, source)
        return MappingDecision(name=name, category_fk=category_fk, source=source)
