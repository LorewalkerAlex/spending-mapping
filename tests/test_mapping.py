"""映射决策（从零）：resolve 兜底、apply 写日志、重放、校验。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spending_mapping.categories import load_categories, load_fallback_pk
from spending_mapping.mapping import MappingStore


class MappingStoreTests(unittest.TestCase):
    def setUp(self):
        self.categories = load_categories()
        self.tmp = Path(tempfile.mkdtemp(prefix="mapping-"))
        self.log = self.tmp / "review-log.jsonl"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self) -> MappingStore:
        return MappingStore.load(self.categories, self.log)

    def test_resolve_without_rule_uses_display_name_and_fallback(self):
        decision = self._store().resolve("财付通-上海全家便利店", "上海全家便利店")
        self.assertEqual(decision.name, "上海全家便利店")
        self.assertEqual(decision.category_fk, load_fallback_pk())
        self.assertEqual(decision.source, "auto")

    def test_apply_edited_then_resolve(self):
        store = self._store()
        store.apply("财付通-上海全家便利店", name="全家便利店", category_fk="2", action="edited")
        decision = store.resolve("财付通-上海全家便利店", "上海全家便利店")
        self.assertEqual(decision.name, "全家便利店")
        self.assertEqual(decision.category_fk, "2")
        self.assertEqual(decision.source, "human_edited")

    def test_apply_approved_sets_source(self):
        store = self._store()
        decision = store.apply("京东支付-绿联", name="绿联", category_fk="3", action="approved")
        self.assertEqual(decision.source, "human_approved")

    def test_replay_from_log(self):
        store = self._store()
        store.apply("A", name="甲", category_fk="1", action="edited")
        store.apply("B", name="乙", category_fk="2", action="approved")
        # 重新加载，等于重放日志
        store2 = self._store()
        self.assertEqual(store2.resolve("A", "甲").category_fk, "1")
        self.assertEqual(store2.resolve("B", "乙").category_fk, "2")
        self.assertEqual(store2.resolve("C", "丙").category_fk, load_fallback_pk())

    def test_log_is_append_only(self):
        store = self._store()
        store.apply("A", name="甲", category_fk="1", action="edited")
        store.apply("A", name="甲改", category_fk="2", action="edited")
        lines = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["category_fk"], "1")
        self.assertEqual(lines[1]["category_fk"], "2")

    def test_invalid_category_raises(self):
        store = self._store()
        with self.assertRaises(ValueError):
            store.apply("A", name="甲", category_fk="999", action="edited")

    def test_invalid_action_raises(self):
        store = self._store()
        with self.assertRaises(ValueError):
            store.apply("A", name="甲", category_fk="1", action="bogus")


if __name__ == "__main__":
    unittest.main()
