"""Cashew 分类与钱包的口径。

分类 pk 是字符串，可能是 "1"–"11"（默认分类）也可能是 UUID（用户新建），
统一按字符串处理，不做整数判断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
CATEGORIES_FILE = CONFIG_DIR / "categories.json"
CATEGORY_MAP_FILE = CONFIG_DIR / "category-map.json"


class UnknownCategoryError(KeyError):
    """导入集里的分类名在映射表中找不到对应 pk。"""


@dataclass(frozen=True, slots=True)
class Category:
    pk: str
    name: str
    income: bool
    order: int


@dataclass(frozen=True, slots=True)
class Wallet:
    pk: str
    name: str
    currency: str
    decimals: int


def load_categories(path: Path = CATEGORIES_FILE) -> list[Category]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Category(pk=item["pk"], name=item["name"], income=item["income"], order=item["order"])
        for item in raw["categories"]
    ]


def load_wallet(path: Path = CATEGORIES_FILE) -> Wallet:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))["wallet"]
    return Wallet(
        pk=raw["pk"], name=raw["name"], currency=raw["currency"], decimals=raw["decimals"]
    )


def load_category_map(path: Path = CATEGORY_MAP_FILE) -> dict[str, str]:
    """旧分类名 → Cashew 分类 pk。"""
    return dict(json.loads(Path(path).read_text(encoding="utf-8"))["map"])


def load_fallback_pk(path: Path = CATEGORY_MAP_FILE) -> str:
    """未映射商户的兜底分类（当前为「其他」）。"""
    return str(json.loads(Path(path).read_text(encoding="utf-8"))["fallback"])


def resolve_category(category_name: str, mapping: dict[str, str] | None = None) -> str:
    mapping = mapping if mapping is not None else load_category_map()
    try:
        return mapping[category_name]
    except KeyError as exc:
        raise UnknownCategoryError(category_name) from exc


def resolve_category_or_fallback(
    category_name: str,
    mapping: dict[str, str] | None = None,
    fallback_pk: str | None = None,
) -> tuple[str, bool]:
    """返回 (pk, 是否用了兜底)。

    分类名为空或未收录都走兜底——`categoryFk` 是外键，不能留空；
    这些交易本来就要进人工审核队列，兜底只是让账面先成立。
    """
    mapping = mapping if mapping is not None else load_category_map()
    fallback_pk = fallback_pk or load_fallback_pk()
    resolved = mapping.get(category_name or "")
    if resolved is None:
        return fallback_pk, True
    return resolved, False
