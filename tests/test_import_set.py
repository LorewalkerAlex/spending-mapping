"""真实导入集上的守恒检查（需要 net-transactions.json，缺失时跳过）。

从零起步后，net-transactions.json 只作「历史净额结果」的校验基准，不再作为映射输入：
分类一律落兜底「其他」，名字用解析器的 display_name。
"""

from __future__ import annotations

import unittest
from collections import Counter
from decimal import Decimal

from spending_mapping.cashew_payload import build_payload
from spending_mapping.categories import load_categories, load_fallback_pk
from spending_mapping.parser import display_name
from spending_mapping.records import load_transactions
from spending_mapping.timestamps import TimestampSequence
from tests.support import require_import_set


class ImportSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = require_import_set()
        cls.rows = load_transactions(cls.path)

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
        cls.fallback = load_fallback_pk()
        cls.pks = {c.pk for c in load_categories()}
        cls.seq = TimestampSequence()
        # 从零：名字用 display_name，分类一律兜底
        cls.payloads = [
            build_payload(
                row,
                category_pk=cls.fallback,
                modified_ms=cls.seq.next(),
                name=display_name(row.description),
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

    def test_all_categories_fall_back_initially(self):
        counts = Counter(p["categoryFk"] for p in self.payloads)
        self.assertEqual(set(counts), {self.fallback})
        self.assertEqual(sum(counts.values()), len(self.rows))

    def test_amounts_match_source_amounts(self):
        for payload, row in zip(self.payloads, self.rows):
            self.assertAlmostEqual(payload["amount"], float(row.amount), places=2)

    def test_modified_timestamps_strictly_increasing(self):
        stamps = [p["dateTimeModified"] for p in self.payloads]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), len(stamps))

    def test_name_uses_display_name(self):
        """从零起步：name 用解析器的 display_name，不是旧的 merchant 字段。"""
        for payload, row in zip(self.payloads, self.rows):
            expected = display_name(row.description)
            self.assertEqual(payload["name"], expected[:250], row.source_record_id)


if __name__ == "__main__":
    unittest.main()
