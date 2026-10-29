"""Cashew payload 的字段契约。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from spending_mapping.cashew_payload import (
    METHOD_ADDED_EMAIL,
    NOTE_LIMIT,
    NOTE_TRUNCATE,
    build_change,
    build_payload,
    payload_digest,
)
from spending_mapping.records import ImportTransaction

TZ = timezone(timedelta(hours=8))

EXPECTED_KEYS = [
    "transactionPk",
    "pairedTransactionFk",
    "name",
    "amount",
    "note",
    "categoryFk",
    "subCategoryFk",
    "walletFk",
    "dateCreated",
    "dateTimeModified",
    "originalDateDue",
    "income",
    "periodLength",
    "reoccurrence",
    "endDate",
    "upcomingTransactionNotification",
    "type",
    "paid",
    "createdAnotherFutureTransaction",
    "skipPaid",
    "methodAdded",
    "transactionOwnerEmail",
    "transactionOriginalOwnerEmail",
    "sharedKey",
    "sharedOldKey",
    "sharedStatus",
    "sharedDateUpdated",
    "sharedReferenceBudgetPk",
    "objectiveFk",
    "objectiveLoanFk",
    "budgetFksExclude",
]


def sample(**overrides) -> ImportTransaction:
    base = dict(
        source_record_id="src_52b5709348e979cd0f54f549",
        date="2025-08-09",
        merchant="比斯特上海购物村",
        category="综合购物",
        amount=Decimal("-297.00"),
        gross=Decimal("297.00"),
        offset=Decimal("0"),
        note="财付通-唯泰精品购物村（上海）有限公",
        description="财付通-唯泰精品购物村（上海）有限公",
    )
    base.update(overrides)
    return ImportTransaction(**base)


class PayloadShapeTests(unittest.TestCase):
    def test_has_exactly_31_fields(self):
        payload = build_payload(sample(), category_pk="3", modified_ms=1793000000001)
        self.assertEqual(len(payload), 31)
        self.assertEqual(list(payload.keys()), EXPECTED_KEYS)

    def test_identity_fields(self):
        payload = build_payload(sample(), category_pk="3", modified_ms=1793000000001)
        self.assertEqual(payload["transactionPk"], "src_52b5709348e979cd0f54f549")
        self.assertEqual(payload["categoryFk"], "3")
        self.assertEqual(payload["walletFk"], "0")

    def test_amount_is_float_and_negative_for_expense(self):
        payload = build_payload(sample(), category_pk="3", modified_ms=1)
        self.assertIsInstance(payload["amount"], float)
        self.assertEqual(payload["amount"], -297.0)
        self.assertLess(payload["amount"], 0)
        self.assertFalse(payload["income"])

    def test_dates_are_milliseconds_at_local_midnight(self):
        payload = build_payload(sample(), category_pk="3", modified_ms=1793000000001)
        expected = int(datetime(2025, 8, 9, tzinfo=TZ).timestamp() * 1000)
        self.assertEqual(payload["dateCreated"], expected)
        self.assertEqual(payload["originalDateDue"], expected)
        self.assertIsInstance(payload["dateCreated"], int)

    def test_one_off_expense_matches_client_defaults(self):
        payload = build_payload(sample(), category_pk="3", modified_ms=1)
        self.assertIsNone(payload["type"])
        self.assertTrue(payload["paid"])
        self.assertTrue(payload["skipPaid"])
        self.assertEqual(payload["reoccurrence"], 3)  # monthly，客户端新增普通交易即写此值
        self.assertEqual(payload["periodLength"], 1)
        self.assertIsNone(payload["endDate"])
        self.assertEqual(payload["methodAdded"], METHOD_ADDED_EMAIL)

    def test_sharing_and_budget_fields_are_null(self):
        payload = build_payload(sample(), category_pk="3", modified_ms=1)
        for key in (
            "pairedTransactionFk",
            "subCategoryFk",
            "transactionOwnerEmail",
            "transactionOriginalOwnerEmail",
            "sharedKey",
            "sharedOldKey",
            "sharedStatus",
            "sharedDateUpdated",
            "sharedReferenceBudgetPk",
            "objectiveFk",
            "objectiveLoanFk",
            "budgetFksExclude",
        ):
            self.assertIsNone(payload[key], key)


class LengthLimitTests(unittest.TestCase):
    def test_merchant_truncated_to_name_limit(self):
        payload = build_payload(
            sample(merchant="商" * 400), category_pk="3", modified_ms=1
        )
        self.assertEqual(len(payload["name"]), 250)

    def test_note_truncated_and_within_field_limit(self):
        payload = build_payload(
            sample(description="描" * 900), category_pk="3", modified_ms=1
        )
        self.assertEqual(len(payload["note"]), NOTE_TRUNCATE)
        self.assertLessEqual(len(payload["note"]), NOTE_LIMIT)

    def test_note_keeps_offset_information(self):
        payload = build_payload(
            sample(offset=Decimal("42.50")), category_pk="3", modified_ms=1
        )
        self.assertIn("已抵消 42.50", payload["note"])
        self.assertIn("原价 297.00", payload["note"])

    def test_note_falls_back_to_description(self):
        payload = build_payload(
            sample(note="", description="原始描述"), category_pk="3", modified_ms=1
        )
        self.assertTrue(payload["note"].startswith("原始描述"))


class ChangeAndDigestTests(unittest.TestCase):
    def test_change_wraps_payload_with_same_pk(self):
        payload = build_payload(sample(), category_pk="3", modified_ms=1793000000001)
        change = build_change(payload, 1793000000001)
        self.assertEqual(change["table"], "Transaction")
        self.assertEqual(change["pk"], payload["transactionPk"])
        self.assertFalse(change["deleted"])
        self.assertEqual(change["modifiedAt"], 1793000000001)

    def test_digest_ignores_key_order_but_tracks_content(self):
        a = build_payload(sample(), category_pk="3", modified_ms=1)
        b = dict(reversed(list(a.items())))
        self.assertEqual(payload_digest(a), payload_digest(b))

        c = dict(a, amount=-298.0)
        self.assertNotEqual(payload_digest(a), payload_digest(c))

    def test_digest_ignores_write_time(self):
        # dateTimeModified 是写入时的钟点，不属于业务内容，不能影响"是否要推"
        a = build_payload(sample(), category_pk="3", modified_ms=1000)
        b = build_payload(sample(), category_pk="3", modified_ms=2000)
        self.assertEqual(payload_digest(a), payload_digest(b))


if __name__ == "__main__":
    unittest.main()
