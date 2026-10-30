"""Cashew 分类与钱包的口径。

分类 pk 是字符串，可能是 "1"–"11"（默认分类）也可能是 UUID（用户新建），
统一按字符串处理，不做整数判断。

「其他」是未映射交易的兜底分类，pk 记录在 config/categories.json 的 `fallback` 字段。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# 镜像里的布局是 /app/src/spending_mapping + /app/config；本地是仓库根下的 config。
# 允许 CONFIG_DIR 覆盖，避免依赖目录层级。
_override = os.environ.get("CONFIG_DIR")
CONFIG_DIR = Path(_override) if _override else Path(__file__).resolve().parents[2] / "config"
CATEGORIES_FILE = CONFIG_DIR / "categories.json"


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


def _load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_categories(path: Path = CATEGORIES_FILE) -> list[Category]:
    return [
        Category(pk=item["pk"], name=item["name"], income=item["income"], order=item["order"])
        for item in _load(path)["categories"]
    ]


def load_wallet(path: Path = CATEGORIES_FILE) -> Wallet:
    raw = _load(path)["wallet"]
    return Wallet(
        pk=raw["pk"], name=raw["name"], currency=raw["currency"], decimals=raw["decimals"]
    )


def load_fallback_pk(path: Path = CATEGORIES_FILE) -> str:
    """未映射交易的兜底分类 pk（当前为「其他」）。"""
    return str(_load(path)["fallback"])
