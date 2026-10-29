"""服务运行配置：从环境变量读，带默认值。

命名约定：邮箱相关统一 `MAIL_` 前缀，数据根目录用 `DATA_DIR`。
密钥（邮箱授权码）只走环境变量，不落盘、不进镜像。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    data_dir: Path
    interval_seconds: int

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence" / "cmb-email"

    @classmethod
    def from_env(cls) -> "MailPullConfig":
        address = _env("MAIL_ADDR")
        auth_code = _env("MAIL_AUTH_CODE")
        if not address or not auth_code:
            raise RuntimeError("缺少 MAIL_ADDR 或 MAIL_AUTH_CODE 环境变量")
        return cls(
            host=_env("MAIL_HOST", "imap.163.com"),
            port=int(_env("MAIL_PORT", "993")),
            address=address,
            auth_code=auth_code,
            mailbox=_env("MAIL_MAILBOX", "招行信用卡"),
            subject_keyword=_env("MAIL_SUBJECT_KEYWORD", "招商银行信用卡电子账单"),
            since=_env("MAIL_SINCE", "01-Sep-2025"),
            data_dir=Path(_env("DATA_DIR", "/data")),
            interval_seconds=int(_env("MAIL_POLL_INTERVAL_SECONDS", "900")),
        )
