"""真实导入集上的守恒与覆盖检查（需要 net-transactions.json，缺失时跳过）。"""

from __future__ import annotations

import unittest
from collections import Counter
from decimal import Decimal

from spending_mapping.cashew_payload import build_payload
from spending_mapping.categories import (
    load_categories,
    load_category_map,
    load_fallback_pk,
    resolve_category_or_fallback,
)
from spending_mapping.records import load_transactions
from spending_mapping.timestamps import TimestampSequence
from tests.support import require_import_set


class ImportSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = require_import_set()
        cls.rows = load_transactions(cls.path)
        cls.category_map = load_category_map()
        cls.fallback = load_fallback_pk()
        cls.pks = {c.pk for c in load_categories()}

    def test_row_count_is_1364(self):
        self.assertEqual(len(self.rows), 1364)

    def test_net_total_is_conserved(self):
        total = sum((row.amount for row in self.rows), Decimal("0"))
        self.assertEqual(total, Decimal("-138461.01"))

    def test_all_ids_unique_and_src_formatted(self):
        ids = [row.source_record_id for row in self.rows]
        self.assertEqual(len(ids), len(set(ids)))
        for record_id in ids:
            self.assertTrue(record_id.startswith("src_"), record_id)
            self.assertEqual(len(record_id), 28, record_id)

    def test_every_row_resolves_to_existing_pk(self):
        for row in self.rows:
            pk, _ = resolve_category_or_fallback(row.category, self.category_map, self.fallback)
            self.assertIn(pk, self.pks, row.source_record_id)

    def test_unmapped_merchants_fall_back_to_other(self):
        """未映射商户必须走兜底，并且规模要可见——这批就是审核队列的起始工作量。"""
        fell_back = [
            row
            for row in self.rows
            if resolve_category_or_fallback(row.category, self.category_map, self.fallback)[1]
        ]
        self.assertEqual(len(fell_back), 117, "未映射笔数变化，需同步更新审核队列基线")
        self.assertEqual(
            sum((row.amount for row in fell_back), Decimal("0")), Decimal("-5983.59")
        )
        self.assertTrue(all(not row.category for row in fell_back))

    def test_amounts_are_negative_expenses(self):
        for row in self.rows:
            self.assertLess(row.amount, 0, row.source_record_id)

    def test_offsets_do_not_exceed_gross(self):
        for row in self.rows:
            self.assertLessEqual(row.offset, row.gross, row.source_record_id)
            self.assertGreaterEqual(row.amount, -row.gross, row.source_record_id)


class PayloadOverRealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = require_import_set()
        cls.rows = load_transactions(cls.path)
        cls.category_map = load_category_map()
        cls.fallback = load_fallback_pk()
        cls.pks = {c.pk for c in load_categories()}
        cls.seq = TimestampSequence()
        cls.payloads = [
            build_payload(
                row,
                category_pk=resolve_category_or_fallback(
                    row.category, cls.category_map, cls.fallback
                )[0],
                modified_ms=cls.seq.next(),
            )
            for row in cls.rows
        ]

    def test_field_count_uniform(self):
        self.assertEqual({len(p) for p in self.payloads}, {31})

    def test_name_and_note_within_limits(self):
        for payload in self.payloads:
            self.assertLessEqual(len(payload["name"]), 250, payload["transactionPk"])
            self.assertLessEqual(len(payload["note"]), 500, payload["transactionPk"])
            self.assertTrue(payload["name"], payload["transactionPk"])

    def test_all_payload_pks_match_source_ids(self):
        self.assertEqual(
            [p["transactionPk"] for p in self.payloads],
            [r.source_record_id for r in self.rows],
        )

    def test_category_distribution_only_uses_known_pks(self):
        counts = Counter(p["categoryFk"] for p in self.payloads)
        self.assertTrue(set(counts) <= self.pks, counts)
        self.assertEqual(sum(counts.values()), len(self.rows))

    def test_amounts_match_source_amounts(self):
        for payload, row in zip(self.payloads, self.rows):
            self.assertAlmostEqual(payload["amount"], float(row.amount), places=2)

    def test_modified_timestamps_strictly_increasing(self):
        stamps = [p["dateTimeModified"] for p in self.payloads]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), len(stamps))


if __name__ == "__main__":
    unittest.main()
