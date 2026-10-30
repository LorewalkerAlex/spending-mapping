"""CLI 测试：sync 的 dry-run/apply/limit，reset 只删 src_ 前缀。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

try:
    from spending_mapping import cli
    from spending_mapping.parser import parse_directory
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

from tests.support import require_evidence_dir
from tests.test_cashew_client import FakeCashewHTTPServer


@unittest.skipUnless(_HAS_BS4, "需要 beautifulsoup4")
class CliTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeCashewHTTPServer()
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-"))
        self._env = dict(os.environ)
        os.environ["DATA_DIR"] = str(self.tmp)
        os.environ["EVIDENCE_DIR"] = str(require_evidence_dir())
        os.environ["CASHEW_BASE"] = self.server.base
        os.environ["CASHEW_EMAIL"] = "me@example.com"
        os.environ["CASHEW_PASSWORD"] = "secret"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.server.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        from spending_mapping.cli import dispatch
        return dispatch(argv)

    def test_sync_dry_run_writes_nothing(self):
        rc = self._run(["sync"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.server.state.alive()), 0)

    def test_sync_limit_pushes_subset(self):
        rc = self._run(["sync", "--apply", "--limit", "5"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.server.state.alive()), 5)

    def test_reset_removes_only_imported(self):
        # 先导入 5 笔，再手工插一条用户交易（UUID）
        self._run(["sync", "--apply", "--limit", "5"])
        self.server.state.push([{
            "table": "Transaction", "pk": "11111111-1111-4111-8111-111111111111",
            "deleted": False, "modifiedAt": 9_999_999_999_999,
            "payload": {"transactionPk": "11111111-1111-4111-8111-111111111111", "name": "午饭"},
        }])
        self.assertEqual(len(self.server.state.alive()), 6)

        rc = self._run(["reset"])
        self.assertEqual(rc, 1)  # 没有 --yes
        self.assertEqual(len(self.server.state.alive()), 6)

        rc = self._run(["reset", "--yes"])
        self.assertEqual(rc, 0)
        alive = self.server.state.alive()
        self.assertEqual(list(alive), ["11111111-1111-4111-8111-111111111111"])

    def test_reimport_after_reset(self):
        self._run(["sync", "--apply", "--limit", "5"])
        self._run(["reset", "--yes"])
        self.assertEqual(len(self.server.state.alive()), 0)
        # 重新导入应再次推出 5 笔
        self._run(["sync", "--apply", "--limit", "5"])
        self.assertEqual(len(self.server.state.alive()), 5)


if __name__ == "__main__":
    unittest.main()
