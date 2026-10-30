"""入账台账：事件重放、去重判定、删除状态。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spending_mapping.ledger import DELETED, IMPORTED, ImportLedger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ledger-"))
        self.path = self.tmp / "imported.jsonl"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mark_and_query(self):
        ledger = ImportLedger.load(self.path)
        self.assertFalse(ledger.is_imported("src_a"))
        ledger.mark_imported("src_a", amount="-10.0", name="盒马")
        self.assertTrue(ledger.is_imported("src_a"))
        self.assertEqual(ledger.imported_ids(), {"src_a"})

    def test_deleted_overrides_imported(self):
        ledger = ImportLedger.load(self.path)
        ledger.mark_imported("src_a", amount="-10.0", name="盒马")
        ledger.mark_deleted("src_a")
        self.assertFalse(ledger.is_imported("src_a"))
        self.assertEqual(ledger.imported_ids(), set())

    def test_replay_from_file(self):
        ledger = ImportLedger.load(self.path)
        ledger.mark_imported("src_a", amount="-10.0", name="盒马")
        ledger.mark_imported("src_b", amount="-20.0", name="山姆")
        again = ImportLedger.load(self.path)
        self.assertEqual(again.imported_ids(), {"src_a", "src_b"})
        self.assertEqual(again._state["src_a"].name, "盒马")

    def test_reimport_after_delete_is_allowed(self):
        ledger = ImportLedger.load(self.path)
        ledger.mark_imported("src_a", amount="-10.0", name="盒马")
        ledger.mark_deleted("src_a")
        ledger.mark_imported("src_a", amount="-10.0", name="盒马")
        self.assertTrue(ledger.is_imported("src_a"))

    def test_file_is_append_only(self):
        ledger = ImportLedger.load(self.path)
        ledger.mark_imported("src_a", amount="-10.0", name="盒马")
        ledger.mark_deleted("src_a")
        lines = [line for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertIn(IMPORTED, lines[0])
        self.assertIn(DELETED, lines[1])


if __name__ == "__main__":
    unittest.main()
