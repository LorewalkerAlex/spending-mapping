"""导入路径：解析 → 净额 → 去重 → 推送（不做分类决策）。"""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

try:
    from spending_mapping.parser import parse_directory
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

from spending_mapping.categories import load_fallback_pk
from spending_mapping.ledger import ImportLedger
from spending_mapping.pipeline import build_items, import_transactions
from tests.support import require_evidence_dir


@unittest.skipUnless(_HAS_BS4, "需要 beautifulsoup4")
class ImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.statements = parse_directory(require_evidence_dir())
        cls.fallback = load_fallback_pk()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="import-"))
        self.ledger_path = self.tmp / "imported.jsonl"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_items_from_zero(self):
        items, netted = build_items(self.statements)
        self.assertEqual(len(items), 1366)
        self.assertEqual(len(netted.offsets), 117)
        self.assertEqual(len(netted.discarded), 11)
        for item in items:
            self.assertEqual(item.category_pk, self.fallback)
            self.assertLess(item.txn.amount, 0)
            self.assertTrue(item.name)

    def _client(self, server):
        from spending_mapping.cashew_client import CashewClient
        client = CashewClient(server.base, email="a@b.c", password="x",
                              session_file=self.tmp / ".s")
        client.ensure()
        return client

    def test_dry_run_writes_nothing(self):
        from tests.test_cashew_client import FakeCashewHTTPServer

        server = FakeCashewHTTPServer()
        try:
            client = self._client(server)
            ledger = ImportLedger.load(self.ledger_path)
            result = import_transactions(client, self.statements, ledger, apply=False)
            self.assertEqual(result.planned, 1366)
            self.assertEqual(result.pushed, 0)
            self.assertEqual(len(server.state.alive()), 0)
            self.assertEqual(len(ledger), 0)
        finally:
            server.stop()

    def test_apply_then_reimport_is_idempotent(self):
        from tests.test_cashew_client import FakeCashewHTTPServer

        server = FakeCashewHTTPServer()
        try:
            client = self._client(server)
            ledger = ImportLedger.load(self.ledger_path)

            first = import_transactions(client, self.statements, ledger, apply=True)
            self.assertEqual(first.pushed, 1366)
            self.assertEqual(len(ledger), 1366)
            self.assertEqual(len(server.state.alive()), 1366)

            # 全量再导一次：台账拦住，零推送
            second = import_transactions(client, self.statements, ledger, apply=True)
            self.assertEqual(second.planned, 0)
            self.assertEqual(second.pushed, 0)
            self.assertEqual(second.skipped_by_ledger, 1366)
            self.assertEqual(len(server.state.alive()), 1366)
        finally:
            server.stop()

    def test_limit_takes_next_batch_after_dedup(self):
        """--limit 是在去重之后取前 N 笔，第二次会取到接下来的 N 笔。"""
        from tests.test_cashew_client import FakeCashewHTTPServer

        server = FakeCashewHTTPServer()
        try:
            client = self._client(server)
            ledger = ImportLedger.load(self.ledger_path)
            first = import_transactions(client, self.statements, ledger, apply=True, limit=10)
            self.assertEqual(first.pushed, 10)
            second = import_transactions(client, self.statements, ledger, apply=True, limit=10)
            self.assertEqual(second.pushed, 10)
            self.assertEqual(len(server.state.alive()), 20)
        finally:
            server.stop()

    def test_remote_dedup_without_ledger(self):
        """台账丢了也不重复：远端已有同 pk 就跳过。"""
        from tests.test_cashew_client import FakeCashewHTTPServer

        server = FakeCashewHTTPServer()
        try:
            client = self._client(server)
            import_transactions(client, self.statements, ImportLedger.load(self.ledger_path),
                                apply=True)

            fresh = ImportLedger.load(self.tmp / "fresh.jsonl")
            result = import_transactions(client, self.statements, fresh, apply=True)
            self.assertEqual(result.planned, 0)
            self.assertEqual(result.skipped_by_ledger, 0)
            self.assertEqual(len(server.state.alive()), 1366)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
