"""从 163 邮箱拉取招行信用卡账单邮件，按 sha256 落盘。

只做"取原始邮件字节并落盘"，不做解析、不做映射。幂等：目标目录里已有
相同 sha256 的文件就跳过，所以重复运行不产生新文件。

163 特有细节（照搬已验证的实现，不重写）：
  - 登录后必须先发 IMAP `ID` 握手，否则无法 select 邮箱
  - 非 ASCII 邮箱名用 Modified UTF-7 编码（自带实现，无第三方依赖）
  - 全程 `BODY.PEEK`，不改变邮箱已读状态
  - 服务端 `SENTSINCE` 粗筛 + 客户端按主题关键词与 Date 头再复核

凭据只从环境变量读，不落盘：
  MAIL_ADDR / MAIL_AUTH_CODE（163 的授权码，不是登录密码）
"""

from __future__ import annotations

import base64
import hashlib
import imaplib
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from email import policy
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from pathlib import Path

HEADER_QUERY = "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE MESSAGE-ID)])"
RAW_QUERY = "(BODY.PEEK[])"


def parse_since_date(value: str) -> date:
    """校验 IMAP since 日期格式（DD-Mon-YYYY），在联网前先失败。"""
    try:
        return datetime.strptime(value, "%d-%b-%Y").date()
    except ValueError as exc:
        raise ValueError(f"无效的 IMAP since 值 {value!r}，应为 DD-Mon-YYYY") from exc


def _modified_b64(data: bytes) -> str:
    """Modified base64：用 ',' 代替 '/'，去掉 '=' 填充（RFC 2152 / 3501）。"""
    encoded = base64.b64encode(data).decode("ascii").rstrip("=")
    return encoded.replace("/", ",")


def encode_mailbox(name: str) -> str:
    """IMAP Modified UTF-7 编码（仅编码方向，够邮箱名用）。

    规则：可打印 ASCII 直接透传，`&` 编码成 `&-`；非 ASCII 按 UTF-16BE 取字节，
    用 modified base64 包成 `&…-`。
    """
    out: list[str] = []
    buf = bytearray()

    def flush() -> None:
        if buf:
            out.append("&" + _modified_b64(bytes(buf)) + "-")
            buf.clear()

    for ch in name:
        code = ord(ch)
        if 0x20 <= code <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch.encode("utf-16-be")
    flush()
    return "".join(out)


def _fetch_bytes(mail: object, mail_id: bytes, query: str) -> bytes:
    status, data = mail.fetch(mail_id, query)
    if status == "OK" and data:
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                return item[1]
    raise RuntimeError(f"获取邮件 {mail_id!r} 失败：status={status!r}")


def _send_imap_id(mail: object) -> None:
    imaplib.Commands["ID"] = ("AUTH",)
    payload = '("name" "spending-mapping" "version" "0.1.0" "vendor" "family-spending")'
    status, data = mail._simple_command("ID", payload)  # noqa: SLF001
    if status != "OK":
        raise RuntimeError(f"IMAP ID 握手失败：{data!r}")


def _select_mailbox(mail: object, mailbox: str) -> None:
    encoded = encode_mailbox(mailbox)
    for candidate in dict.fromkeys((encoded, f'"{encoded}"')):
        status, data = mail.select(candidate, readonly=True)
        if status == "OK":
            return
    raise RuntimeError(f"无法选择邮箱 {mailbox!r}")


def _matching_ids(
    mail: object,
    *,
    since: str,
    since_date: date,
    subject_keyword: str,
) -> tuple[bytes, ...]:
    status, data = mail.search(None, "SENTSINCE", since)
    if status != "OK":
        raise RuntimeError(f"IMAP search 失败：{data!r}")
    ids = data[0].split() if data and data[0] else []
    keyword = subject_keyword.casefold()
    matches: list[bytes] = []
    for mail_id in ids:
        raw_header = _fetch_bytes(mail, mail_id, HEADER_QUERY)
        header = BytesHeaderParser(policy=policy.default).parsebytes(raw_header)
        if keyword not in str(header.get("Subject", "")).casefold():
            continue
        raw_date = str(header.get("Date", ""))
        try:
            mail_date = parsedate_to_datetime(raw_date).date()
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"邮件 {mail_id!r} 的 Date 头无效") from exc
        if mail_date >= since_date:
            matches.append(mail_id)
    return tuple(matches)


@dataclass(slots=True)
class PullResult:
    fetched: int = 0
    added: int = 0
    duplicates: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def pull_cmb(
    *,
    host: str,
    port: int,
    address: str,
    auth_code: str,
    mailbox: str,
    subject_keyword: str,
    since: str,
    evidence_dir: Path,
    timeout_seconds: float = 30,
    imap_factory: Callable[..., object] = imaplib.IMAP4_SSL,
) -> PullResult:
    """拉取一批邮件，sha256 命名写入 evidence_dir，返回计数。"""
    since_date = parse_since_date(since)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    result = PullResult()
    mail = imap_factory(host, port, timeout=timeout_seconds)
    logged_in = False
    try:
        status, data = mail.login(address, auth_code)
        if status != "OK":
            raise RuntimeError(f"IMAP 登录失败：{data!r}")
        logged_in = True
        _send_imap_id(mail)
        _select_mailbox(mail, mailbox)
        for mail_id in _matching_ids(
            mail, since=since, since_date=since_date, subject_keyword=subject_keyword
        ):
            raw = _fetch_bytes(mail, mail_id, RAW_QUERY)
            digest = hashlib.sha256(raw).hexdigest()
            target = evidence_dir / f"{digest}.eml"
            result.fetched += 1
            if target.exists():
                result.duplicates += 1
                continue
            target.write_bytes(raw)
            result.added += 1
    except Exception as exc:  # noqa: BLE001 — 拉取失败要能整体记录，不打断上层
        result.errors.append(str(exc))
    finally:
        if logged_in:
            try:
                mail.logout()
            except Exception:  # noqa: BLE001
                pass
    return result


def credentials_from_env(environ: dict | None = None) -> tuple[str, str]:
    values = os.environ if environ is None else environ
    address = values.get("MAIL_ADDR", "").strip()
    auth_code = values.get("MAIL_AUTH_CODE", "").strip()
    if not address or not auth_code:
        raise RuntimeError("缺少 MAIL_ADDR 或 MAIL_AUTH_CODE 环境变量")
    return address, auth_code
