"""解析器的回归测试：对着真实账单邮件跑，断言对账与规模。

需要 *.eml（设置 EVIDENCE_DIR），且环境里有 beautifulsoup4；两者缺一则跳过。
"""

from __future__ import annotations

import unittest
from collections import Counter

try:
    from spending_mapping.parser import parse_directory, parse_statement  # noqa: F401
    _HAS_BS4 = True
except ImportError:  # beautifulsoup4 未安装
    _HAS_BS4 = False

from tests.support import require_evidence_dir


@unittest.skipUnless(_HAS_BS4, "需要 beautifulsoup4")
class ParserRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = require_evidence_dir()
        cls.statements = parse_directory(cls.directory)

    def test_14_statements(self):
        self.assertEqual(len(self.statements), 14)

    def test_1531_records(self):
        total = sum(len(s.records) for s in self.statements)
        self.assertEqual(total, 1531)

    def test_all_statements_reconcile(self):
        failed = [s for s in self.statements if not s.reconcile["all_ok"]]
        self.assertEqual(failed, [], [s.period_start for s in failed])

    def test_section_distribution(self):
        counts = Counter(r.section for s in self.statements for r in s.records)
        self.assertEqual(dict(counts), {
            "消费": 1386, "退款": 125, "还款": 14, "费用": 3, "其他": 3,
        })

    def test_section_signs(self):
        """原始账单的符号规律：消费正、退款/其他/还款负、费用可能为零。

        Cashew 需要「负数=支出」，所以消费在生成 payload 时取负——取负发生在
        净额/payload 环节，不是解析环节。
        """
        for statement in self.statements:
            for record in statement.records:
                if record.section == "消费":
                    self.assertGreater(float(record.amount), 0, record.record_id)
                elif record.section in ("退款", "其他", "还款"):
                    self.assertLess(float(record.amount), 0, record.record_id)

    def test_ids_are_stable_across_reparse(self):
        first = {r.record_id for s in self.statements for r in s.records}
        second = {r.record_id for s in parse_directory(self.directory) for r in s.records}
        self.assertEqual(first, second)

    def test_ids_are_unique(self):
        ids = [r.record_id for s in self.statements for r in s.records]
        self.assertEqual(len(ids), len(set(ids)))

    def test_ids_are_src_format(self):
        for statement in self.statements:
            for record in statement.records:
                self.assertTrue(record.record_id.startswith("src_"), record.record_id)
                self.assertEqual(len(record.record_id), 28, record.record_id)


if __name__ == "__main__":
    unittest.main()
