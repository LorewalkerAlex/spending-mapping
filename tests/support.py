"""测试公共设施：导入集路径与数据加载。"""

from __future__ import annotations

import os
import pathlib
import unittest

# 数据目录从环境变量取；也允许放在仓库内 data/ 下。都不存在则相关用例跳过。
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CANDIDATES = [
    pathlib.Path(p) if p else pathlib.Path()
    for p in [os.environ.get("SPENDING_DATA_DIR")]
    if p
]
CANDIDATES.append(_REPO_ROOT / "data")


def _locate_import_set() -> pathlib.Path | None:
    for base in CANDIDATES:
        candidate = base / "net-transactions.json"
        if candidate.is_file():
            return candidate
    return None


def require_import_set() -> pathlib.Path:
    found = _locate_import_set()
    if found is None:
        raise unittest.SkipTest(
            "缺少 net-transactions.json；设置 SPENDING_DATA_DIR 指向包含它的目录"
        )
    return found
