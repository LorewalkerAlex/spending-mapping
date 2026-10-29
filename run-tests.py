#!/usr/bin/env python3
"""本地测试入口：python run-tests.py [-v]

不依赖 pytest，也不需要安装，只用标准库。数据相关用例在缺少导入集时自动跳过。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def main() -> int:
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), top_level_dir=str(ROOT)
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print()
    print(f"用例 {result.testsRun} 个，失败 {len(result.failures)}，错误 {len(result.errors)}，"
          f"跳过 {len(result.skipped)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
