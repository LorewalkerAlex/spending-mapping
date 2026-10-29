"""服务运行配置：从环境变量读，带默认值。

密钥（邮箱授权码）只走环境变量，不落盘、不进镜像。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(key: str, default: str | None = None) -> str | None:
    value = os.environ.get(key)
    return value if value not in (None, "") else default


@dataclass(frozen=True, slots=True)
class MailPullConfig:
    host: str
    port: int
    address: str
    auth_code: str
    mailbox: str
    subject_keyword: str
    since: str
    evidence_dir: str
    interval_seconds: int

    @classmethod
    def from_env(cls) -> "MailPullConfig":
        address = _env("EMAIL_ADDR")
        auth_code = _env("EMAIL_AUTH_CODE")
        if not address or not auth_code:
            raise RuntimeError("缺少 EMAIL_ADDR 或 EMAIL_AUTH_CODE 环境变量")
        return cls(
            host=_env("MAIL_HOST", "imap.163.com"),
            port=int(_env("MAIL_PORT", "993")),
            address=address,
            auth_code=auth_code,
            mailbox=_env("MAIL_MAILBOX", "招行信用卡"),
            subject_keyword=_env("MAIL_SUBJECT_KEYWORD", "招商银行信用卡电子账单"),
            since=_env("MAIL_SINCE", "01-Sep-2025"),
            evidence_dir=_env("EVIDENCE_DIR", "/data/evidence/cmb-email"),
            interval_seconds=int(_env("EMAIL_POLL_INTERVAL_SECONDS", "900")),
        )
