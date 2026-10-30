"""流水线端到端：14 封真实邮件 → 净额 → 映射 → 推送假服务端 → 回读。"""

from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import mkdtemp

try:
    from spending_mapping.parser import parse_directory
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

from spending_mapping.categories import load_categories, load_fallback_pk
from spending_mapping.mapping import MappingStore
from spending_mapping.pipeline import build_items, run_pipeline
from spending_mapping.timestamps import TimestampSequence
from tests.support import require_evidence_dir


@unittest.skipUnless(_HAS_BS4, "需要 beautifulsoup4")
class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.statements = parse_directory(require_evidence_dir())
        cls.categories = load_categories()
        cls.fallback = load_fallback_pk()

    def setUp(self):
        self.tmp = Path(mkdtemp(prefix="pipeline-"))
        self.mapping = MappingStore.load(self.categories, self.tmp / "review-log.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_items_from_zero(self):
        items, netted = build_items(self.statements, self.mapping)
        self.assertEqual(len(items), 1366)          # 1386 消费 − 20 全额抵消
        self.assertEqual(len(netted.offsets), 117)  # 从零的口径
        self.assertEqual(len(netted.discarded), 11)
        for item in items:
            self.assertEqual(item.category_pk, self.fallback)
            self.assertLess(item.txn.amount, 0)
            self.assertTrue(item.name)

    def test_run_pipeline_pushes_and_reads_back(self):
        from tests.test_cashew_client import FakeCashewHTTPServer
        from spending_mapping.cashew_client import CashewClient

        server = FakeCashewHTTPServer()
        try:
            client = CashewClient(
                server.base,
                email="me@example.com",
                password="secret",
                session_file=self.tmp / ".session.json",
            )
            client.ensure()
            result = run_pipeline(client, self.statements, self.mapping, batch_size=200)
            self.assertEqual(result.net_rows, 1366)
            self.assertEqual(result.unmatched, 11)
            self.assertEqual(result.pushed, 1366)
            self.assertEqual(result.conflicts, 0)

            # 回读：假服务端里应有 1366 条，全部落兜底分类
            alive = server.state.alive()
            self.assertEqual(len(alive), 1366)
            pks = {str(record["payload"].get("categoryFk")) for record in alive.values()}
            self.assertEqual(pks, {self.fallback})

            # 幂等：再跑一次不该再推
            again = run_pipeline(client, self.statements, self.mapping)
            self.assertEqual(again.planned, 0)
        finally:
            server.stop()

    def test_unmatched_total_is_reported(self):
        _, netted = build_items(self.statements, self.mapping)
        total = -sum((Decimal(r.amount) for r in netted.discarded), Decimal(0))
        self.assertGreater(total, 0)


if __name__ == "__main__":
    unittest.main()
