"""邮件拉取（IMAP → sha256 落盘）的假 IMAP 测试。

不连真实邮箱。用一个内存假 IMAP 客户端验证：过滤、去重、落盘、异常记录。
"""

from __future__ import annotations

import email.utils
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spending_mapping.mail_pull import encode_mailbox, pull_cmb


class ModifiedUtf7Tests(unittest.TestCase):
    def test_ascii_passthrough(self):
        self.assertEqual(encode_mailbox("INBOX"), "INBOX")
        self.assertEqual(encode_mailbox("Spam 2025"), "Spam 2025")

    def test_ampersand_is_escaped(self):
        self.assertEqual(encode_mailbox("A&B"), "A&-B")

    def test_non_ascii_is_encoded(self):
        # "招" U+62DB → UTF-16BE 0x62DB → base64 "Yts" → "&Yts-"
        self.assertEqual(encode_mailbox("招"), "&Yts-")
        self.assertTrue(encode_mailbox("招行信用卡").startswith("&"))

    def test_roundtrip_is_ascii(self):
        encoded = encode_mailbox("招行信用卡")
        encoded.encode("ascii")  # 不应抛异常


def make_eml(subject: str, sent_at: datetime) -> bytes:
    # Message-ID 用 subject 派生，保证同一封邮件字节稳定，去重逻辑才能被测
    import hashlib

    msg_id = f"<{hashlib.sha256(subject.encode()).hexdigest()[:16]}@example.com>"
    date_header = email.utils.format_datetime(sent_at)
    return (
        f"From: billing@cmb.example.com\r\n"
        f"To: me@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date_header}\r\n"
        f"Message-ID: {msg_id}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"\r\n"
        f"<html><body>账单</body></html>\r\n"
    ).encode("utf-8")


class FakeIMAP:
    """实现 pull_cmb 用到的 imaplib 表面：login/select/search/fetch/_simple_command/logout。"""

    def __init__(self, messages: dict[str, bytes]):
        self._messages = messages  # subject -> raw bytes
        self.logged_in = False
        self.selected = None
        self.fetch_calls = []

    def login(self, address, code):
        self.logged_in = True
        return "OK", [b""]

    def _simple_command(self, cmd, payload):
        return "OK", b""

    def select(self, name, readonly=True):
        self.selected = name
        return "OK", [b"1"]

    def search(self, *args):
        return "OK", [b" ".join(f"{i}".encode() for i in range(1, len(self._messages) + 1))]

    def fetch(self, mail_id, query):
        self.fetch_calls.append((mail_id, query))
        subject = list(self._messages.keys())[int(mail_id) - 1]
        raw = self._messages[subject]
        if "HEADER.FIELDS" in query:
            header = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            return "OK", [(b"1 (BODY[HEADER])", header)]
        return "OK", [(b"1 (BODY[])", raw)]

    def logout(self):
        self.logged_in = False
        return "OK", [b""]


def _since_date() -> datetime:
    return datetime(2025, 8, 1, tzinfo=timezone.utc)


class MailPullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.id())
        # 用临时目录，测试结束清理
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="mailpull-"))
        self.evidence = self.tmp / "evidence"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _messages(self):
        return {
            "招商银行信用卡电子账单 2025年8月": make_eml(
                "招商银行信用卡电子账单 2025年8月", _since_date() + timedelta(days=10)
            ),
            "招商银行信用卡电子账单 2025年7月": make_eml(
                "招商银行信用卡电子账单 2025年7月", _since_date() + timedelta(days=5)
            ),
            "普通促销邮件 无关": make_eml("普通促销邮件 无关", _since_date() + timedelta(days=3)),
            "招商银行信用卡电子账单 旧账": make_eml(
                "招商银行信用卡电子账单 旧账", _since_date() - timedelta(days=30)
            ),
        }

    def _run(self, messages=None, **overrides):
        messages = messages or self._messages()
        seen = {}
        fake = FakeIMAP(messages)
        result = pull_cmb(
            host="imap.163.com",
            port=993,
            address="me@163.com",
            auth_code="authcode",
            mailbox="招行信用卡",
            subject_keyword="招商银行信用卡电子账单",
            since="01-Aug-2025",
            evidence_dir=self.evidence,
            imap_factory=lambda *a, **k: fake,
            **overrides,
        )
        return result, fake

    def test_filters_subject_and_since_date(self):
        result, _ = self._run()
        # 命中：8月、7月 两封账单（旧账被日期滤掉，促销被主题滤掉）
        self.assertEqual(result.fetched, 2)
        self.assertEqual(result.added, 2)
        self.assertEqual(result.duplicates, 0)
        self.assertEqual(result.errors, [])

    def test_writes_sha256_named_files(self):
        result, _ = self._run()
        files = list(self.evidence.glob("*.eml"))
        self.assertEqual(len(files), 2)
        for path in files:
            self.assertEqual(len(path.name), 64 + 4)  # 64 hex + ".eml"

    def test_second_run_is_duplicate(self):
        self._run()
        result, _ = self._run()
        self.assertEqual(result.fetched, 2)
        self.assertEqual(result.added, 0)
        self.assertEqual(result.duplicates, 2)

    def test_login_failure_is_recorded_not_raised(self):
        class FailingIMAP(FakeIMAP):
            def login(self, address, code):
                return "NO", [b"bad credentials"]

        result = pull_cmb(
            host="imap.163.com", port=993, address="x", auth_code="x",
            mailbox="招行信用卡", subject_keyword="招商银行信用卡电子账单",
            since="01-Aug-2025", evidence_dir=self.evidence,
            imap_factory=lambda *a, **k: FailingIMAP(self._messages()),
        )
        self.assertEqual(result.fetched, 0)
        self.assertTrue(result.errors)

    def test_uses_peek_queries(self):
        _, fake = self._run()
        queries = {q for _, q in fake.fetch_calls}
        self.assertTrue(all("BODY.PEEK" in q for q in queries))


if __name__ == "__main__":
    unittest.main()
