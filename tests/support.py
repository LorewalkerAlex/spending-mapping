"""测试公共设施：导入集路径与数据加载。"""

from __future__ import annotations

import os
import pathlib
import unittest

DEFAULT_DATA_DIR = pathlib.Path("F:/OtherProjects/cashew-deploy-20261027")
DATA_DIR = pathlib.Path(os.environ.get("SPENDING_DATA_DIR", DEFAULT_DATA_DIR))
IMPORT_SET = DATA_DIR / "net-transactions.json"


def require_import_set() -> pathlib.Path:
    if not IMPORT_SET.is_file():
        raise unittest.SkipTest(
            f"缺少导入集 {IMPORT_SET}；设置 SPENDING_DATA_DIR 指向包含 net-transactions.json 的目录"
        )
    return IMPORT_SET
