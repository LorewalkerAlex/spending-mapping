"""分类与钱包口径。"""

from __future__ import annotations

import unittest

from spending_mapping.categories import (
    load_categories,
    load_fallback_pk,
    load_wallet,
)


class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.categories = load_categories()
        self.by_pk = {c.pk: c for c in self.categories}

    def test_14_categories(self):
        self.assertEqual(len(self.categories), 14)

    def test_default_pks_are_numeric_strings_and_extras_are_uuids(self):
        numeric = sorted(int(c.pk) for c in self.categories if c.pk.isdigit())
        self.assertEqual(numeric, list(range(1, 12)))  # 默认分类 "1"–"11"
        uuid_like = [c.pk for c in self.categories if c.pk.count("-") == 4]
        self.assertEqual(len(uuid_like), 3)

    def test_names_are_unique(self):
        names = [c.name for c in self.categories]
        self.assertEqual(len(names), len(set(names)))

    def test_only_income_category(self):
        income = [c.name for c in self.categories if c.income]
        self.assertEqual(income, ["收入"])

    def test_every_pk_is_non_empty_string(self):
        for category in self.categories:
            self.assertIsInstance(category.pk, str)
            self.assertTrue(category.pk.strip())


class FallbackTests(unittest.TestCase):
    def test_fallback_is_other_category(self):
        fallback = load_fallback_pk()
        by_pk = {c.pk: c for c in load_categories()}
        self.assertIn(fallback, by_pk)
        self.assertEqual(by_pk[fallback].name, "其他")


class WalletTests(unittest.TestCase):
    def test_wallet_is_bank_account(self):
        wallet = load_wallet()
        self.assertEqual(wallet.pk, "0")
        self.assertEqual(wallet.currency, "cny")
        self.assertEqual(wallet.decimals, 2)


if __name__ == "__main__":
    unittest.main()
