"""审核前端的接口测试：起一个真实 HTTP 服务，打到假 Cashew。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

try:
    from spending_mapping.parser import parse_directory
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

from spending_mapping.ledger import ImportLedger
from spending_mapping.pipeline import import_transactions
from tests.support import require_evidence_dir
from tests.test_cashew_client import FakeCashewHTTPServer


@unittest.skipUnless(_HAS_BS4, "需要 beautifulsoup4")
class WebApiTests(unittest.TestCase):
    def setUp(self):
        from spending_mapping.cashew_client import CashewClient
        from spending_mapping.web import ReviewServer

        self.cashew = FakeCashewHTTPServer()
        self.tmp = Path(tempfile.mkdtemp(prefix="web-"))
        self.statements = parse_directory(require_evidence_dir())

        self.cashew_client = CashewClient(self.cashew.base, email="a@b.c", password="x",
                                          session_file=self.tmp / ".s")
        self.cashew_client.ensure()
        import_transactions(self.cashew_client, self.statements,
                            ImportLedger.load(self.tmp / "imported.jsonl"),
                            apply=True, limit=6)

        self.server = ReviewServer(("127.0.0.1", 0), client_factory=lambda: self.cashew_client)
        self.server.data_dir = self.tmp
        self.server.evidence_dir = require_evidence_dir()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.cashew.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=30) as resp:
            return json.loads(resp.read())

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def test_index_returns_html(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as resp:
            html = resp.read().decode()
        self.assertIn("账单审核", html)
        self.assertIn("/api/pending", html)

    def test_pending_payload(self):
        data = self._get("/api/pending")
        self.assertEqual(len(data["categories"]), 14)
        self.assertGreater(len(data["merchants"]), 0)
        self.assertEqual(len(data["refunds"]), 11)
        merchant = data["merchants"][0]
        self.assertIn("name", merchant)
        self.assertIn("total", merchant)
        # 建议值要么为空，要么是合法 pk
        pks = {c["pk"] for c in data["categories"]}
        self.assertTrue(merchant["suggested_fk"] is None or merchant["suggested_fk"] in pks)

    def test_apply_categories_and_queue_shrinks(self):
        before = self._get("/api/pending")["merchants"]
        target = before[0]["name"]
        result = self._post("/api/apply-categories",
                            {"decisions": [{"name": target, "category_fk": "2"}]})
        self.assertGreater(result["changed"], 0)
        after = self._get("/api/pending")["merchants"]
        self.assertNotIn(target, {m["name"] for m in after})

    def test_apply_refund_income(self):
        refund = self._get("/api/pending")["refunds"][0]
        self._post("/api/apply-refund",
                   {"record_id": refund["record_id"], "action": "income", "category_fk": "11"})
        after = self._get("/api/pending")["refunds"]
        self.assertNotIn(refund["record_id"], {r["record_id"] for r in after})


if __name__ == "__main__":
    unittest.main()
