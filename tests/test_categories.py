"""分类与钱包口径。"""

from __future__ import annotations

import unittest

from spending_mapping.categories import (
    UnknownCategoryError,
    load_categories,
    load_category_map,
    load_wallet,
    resolve_category,
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


class CategoryMapTests(unittest.TestCase):
    def setUp(self):
        self.mapping = load_category_map()
        self.pks = {c.pk for c in load_categories()}

    def test_every_target_pk_exists(self):
        for name, pk in self.mapping.items():
            self.assertIn(pk, self.pks, f"{name} → {pk} 不是有效分类")

    def test_resolve_known(self):
        self.assertEqual(resolve_category("餐饮美食", self.mapping), "1")
        self.assertEqual(resolve_category("医疗健康", self.mapping),
                         "acf73b07-5da1-4998-a5c0-1b17d5040e9d")

    def test_resolve_unknown_raises(self):
        with self.assertRaises(UnknownCategoryError):
            resolve_category("不存在的分类", self.mapping)


class WalletTests(unittest.TestCase):
    def test_wallet_is_bank_account(self):
        wallet = load_wallet()
        self.assertEqual(wallet.pk, "0")
        self.assertEqual(wallet.currency, "cny")
        self.assertEqual(wallet.decimals, 2)


if __name__ == "__main__":
    unittest.main()
