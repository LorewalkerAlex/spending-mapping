"""CLI 测试：import 的 dry-run/limit、review 列表、apply-category、reset 边界。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

try:
    from spending_mapping.cli import dispatch
    from spending_mapping.parser import parse_directory  # noqa: F401
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

    def test_import_dry_run_writes_nothing(self):
        self.assertEqual(dispatch(["import"]), 0)
        self.assertEqual(len(self.server.state.alive()), 0)

    def test_import_limit(self):
        self.assertEqual(dispatch(["import", "--apply", "--limit", "5"]), 0)
        self.assertEqual(len(self.server.state.alive()), 5)

    def test_review_lists_pending(self):
        dispatch(["import", "--apply", "--limit", "5"])
        self.assertEqual(dispatch(["review"]), 0)

    def test_apply_category_via_cli(self):
        dispatch(["import", "--apply", "--limit", "5"])
        alive = self.server.state.alive()
        name = list(alive.values())[0]["payload"]["name"]
        self.assertEqual(dispatch(["apply-category", "--name", name, "--category", "2", "--apply"]), 0)
        for record in self.server.state.alive().values():
            if record["payload"]["name"] == name:
                self.assertEqual(record["payload"]["categoryFk"], "2")

    def test_reset_keeps_user_transactions(self):
        dispatch(["import", "--apply", "--limit", "5"])
        self.server.state.push([{
            "table": "Transaction", "pk": "11111111-1111-4111-8111-111111111111",
            "deleted": False, "modifiedAt": 9_999_999_999_999,
            "payload": {"transactionPk": "11111111-1111-4111-8111-111111111111", "name": "午饭"},
        }])
        self.assertEqual(dispatch(["reset"]), 1)  # 未加 --yes
        self.assertEqual(dispatch(["reset", "--yes"]), 0)
        self.assertEqual(
            list(self.server.state.alive()),
            ["11111111-1111-4111-8111-111111111111"],
        )

    def test_reimport_after_reset(self):
        dispatch(["import", "--apply", "--limit", "5"])
        dispatch(["reset", "--yes"])
        self.assertEqual(len(self.server.state.alive()), 0)
        dispatch(["import", "--apply", "--limit", "5"])
        self.assertEqual(len(self.server.state.alive()), 5)


if __name__ == "__main__":
    unittest.main()
