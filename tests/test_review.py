"""审核：分类队列与应用、退款队列与两种处理动作。"""

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
from spending_mapping.pipeline import import_transactions
from spending_mapping.review import (
    apply_category,
    apply_refund_income,
    apply_refund_offset,
    list_pending_categories,
    list_pending_refunds,
)
from spending_mapping.review_log import ReviewLog
from tests.support import require_evidence_dir


@unittest.skipUnless(_HAS_BS4, "需要 beautifulsoup4")
class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.statements = parse_directory(require_evidence_dir())
        cls.fallback = load_fallback_pk()

    def setUp(self):
        from tests.test_cashew_client import FakeCashewHTTPServer
        from spending_mapping.cashew_client import CashewClient

        self.server = FakeCashewHTTPServer()
        self.tmp = Path(tempfile.mkdtemp(prefix="review-"))
        self.client = CashewClient(self.server.base, email="a@b.c", password="x",
                                   session_file=self.tmp / ".s")
        self.client.ensure()
        self.ledger = ImportLedger.load(self.tmp / "imported.jsonl")
        self.log = ReviewLog.load(self.tmp / "review-log.jsonl")
        import_transactions(self.client, self.statements, self.ledger, apply=True, limit=12)

    def tearDown(self):
        self.server.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pending_categories_excludes_decided(self):
        items = list_pending_categories(self.client, self.log)
        total = sum(item.count for item in items)
        self.assertEqual(total, 12)
        name = items[0].name
        apply_category(self.client, name, "2", self.log)
        after = list_pending_categories(self.client, self.log)
        self.assertNotIn(name, {item.name for item in after})

    def test_apply_category_updates_cashew(self):
        item = list_pending_categories(self.client, self.log)[0]
        changed = apply_category(self.client, item.name, "2", self.log)
        self.assertEqual(changed, item.count)
        alive = self.server.state.alive()
        for record in alive.values():
            if record["payload"]["name"] == item.name:
                self.assertEqual(record["payload"]["categoryFk"], "2")
        self.assertEqual(self.log.category_for(item.name), "2")

    def test_pending_refunds_lists_unmatched(self):
        refunds = list_pending_refunds(self.statements, self.log)
        self.assertEqual(len(refunds), 11)
        self.assertTrue(all(item.amount < 0 for item in refunds))

    def test_refund_income_creates_positive_transaction(self):
        refund = list_pending_refunds(self.statements, self.log)[0]
        apply_refund_income(self.client, self.statements, refund.record_id, self.fallback, self.log)
        record = self.server.state.alive()[refund.record_id]
        self.assertGreater(record["payload"]["amount"], 0)
        self.assertTrue(record["payload"]["income"])
        self.assertIsNotNone(self.log.refund_action(refund.record_id))
        # 处理过的退款不再出现在队列
        left = {item.record_id for item in list_pending_refunds(self.statements, self.log)}
        self.assertNotIn(refund.record_id, left)

    def test_refund_offset_reduces_target(self):
        refund = list_pending_refunds(self.statements, self.log)[0]
        target_pk = "src_target_for_offset_test"
        self.server.state.push([{
            "table": "Transaction", "pk": target_pk, "deleted": False,
            "modifiedAt": 1_000, "payload": {"transactionPk": target_pk, "amount": -1000.0},
        }])
        apply_refund_offset(self.client, self.statements, refund.record_id, target_pk, self.log)
        updated = self.server.state.alive()[target_pk]["payload"]["amount"]
        self.assertAlmostEqual(updated, float(Decimal("-1000") - refund.amount), places=2)


if __name__ == "__main__":
    unittest.main()
