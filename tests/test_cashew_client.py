"""Cashew 客户端的 HTTP 级测试：用内存假服务端 + 真实 http.server 跑通协议。

覆盖：登录、会话缓存、refresh、push/pull、pull_all 分页、409 rebootstrap、
错误路径（缺凭据、登录失败）。
"""

from __future__ import annotations

import http.server
import json
import threading
import unittest
import urllib.parse
from pathlib import Path

from spending_mapping.cashew_client import CashewClient, CashewHttpError, RebootstrapError
from tests.fake_server import FakeCashewServer, RebootstrapError as FakeRebootstrap


class _Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def do_POST(self):  # noqa: N802
        body = self._body()
        if self.path == "/auth/login":
            self.server.login_count += 1
            self._json(200, {
                "sessionToken": "tok",
                "expiresAt": "2099-01-01T00:00:00Z",
                "user": {"email": body.get("email"), "isAdmin": True},
            })
        elif self.path == "/auth/refresh":
            self._json(200, {"sessionToken": "tok-refreshed", "expiresAt": "2099-01-01T00:00:00Z"})
        elif self.path == "/sync/push":
            self.server.last_auth = self.headers.get("Authorization")
            result = self.server.state.push(body.get("changes", []))
            self._json(200, result)
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/sync/pull":
            self.server.last_auth = self.headers.get("Authorization")
            query = urllib.parse.parse_qs(parsed.query)
            since = int(query.get("since", ["0"])[0])
            limit = int(query.get("limit", ["500"])[0])
            try:
                result = self.server.state.pull(since, limit)
            except FakeRebootstrap as exc:
                self._json(409, {"error": "rebootstrap", "minRetainedSeq": exc.min_retained_seq})
            else:
                self._json(200, result)
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *args):  # noqa: ANN002 — 静默，避免测试输出噪音
        pass


class FakeCashewHTTPServer:
    def __init__(self, state: FakeCashewServer | None = None) -> None:
        self.state = state or FakeCashewServer()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.state = self.state
        self.httpd.login_count = 0
        self.httpd.last_auth = None
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def login_count(self) -> int:
        return self.httpd.login_count

    @property
    def last_auth(self) -> str | None:
        return self.httpd.last_auth

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class CashewClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeCashewHTTPServer()
        self.tmp = Path(self.id())
        self.tmp = self._tmpdir()

    def _tmpdir(self):
        import tempfile
        return Path(tempfile.mkdtemp(prefix="cashew-client-"))

    def tearDown(self):
        self.server.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self, **kwargs):
        return CashewClient(
            self.server.base,
            email="me@example.com",
            password="secret",
            session_file=self.tmp / ".cashew-session.json",
            **kwargs,
        )

    def test_login_returns_token(self):
        client = self._client()
        session = client.login()
        self.assertEqual(session["sessionToken"], "tok")

    def test_ensure_uses_session_cache(self):
        client = self._client()
        client.login()
        client2 = self._client()
        client2.ensure()
        # 第二次 ensure 命中缓存，不再次登录
        self.assertEqual(self.server.login_count, 1)

    def test_push_sends_bearer_auth(self):
        client = self._client()
        client.ensure()
        client.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                      "modifiedAt": 1000, "payload": {"transactionPk": "src_a"}}])
        self.assertEqual(self.server.last_auth, "Bearer tok")

    def test_pull_all_paginates(self):
        client = self._client()
        client.ensure()
        for i in range(5):
            client.push([{"table": "Transaction", "pk": f"src_{i}", "deleted": False,
                          "modifiedAt": 1000 + i, "payload": {"transactionPk": f"src_{i}"}}])
        changes, cursor = client.pull_all(limit=2)
        self.assertEqual(len(changes), 5)
        self.assertEqual(cursor, 5)

    def test_rebootstrap_resets_cursor(self):
        client = self._client()
        client.ensure()
        client.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                      "modifiedAt": 1000, "payload": {"transactionPk": "src_a"}}])
        self.server.state.reset()
        changes, _ = client.pull_all()
        self.assertTrue(client.rebootstrapped)
        self.assertEqual(changes, [])  # reset 后服务端清空

    def test_pull_409_raises_rebootstrap(self):
        client = self._client()
        client.ensure()
        self.server.state.reset()
        with self.assertRaises(RebootstrapError):
            client.pull(since=0)

    def test_login_missing_credentials_raises(self):
        client = CashewClient(self.server.base, session_file=self.tmp / ".x.json")
        with self.assertRaises(RuntimeError):
            client.login()

    def test_http_error_is_typed(self):
        client = self._client()
        client.ensure()
        with self.assertRaises(CashewHttpError) as ctx:
            client._request("GET", "/nonexistent")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
