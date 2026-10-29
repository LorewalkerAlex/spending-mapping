"""Cashew 同步 HTTP 客户端。

只做协议层：登录/刷新、拉取、推送、会话缓存。不参与业务（解析/映射/净额）。

语义依据 docs/CASHEW-SYNC-API.md：
  - `/auth/login` → sessionToken（30 天）、`/auth/refresh` 滑动续期
  - `/sync/pull?since=<seq>&limit=` → changes/nextCursor/hasMore/serverTime
  - `since` 低于 `min_retained_seq` → HTTP 409 rebootstrap
  - `/sync/push` → {serverTime, conflictCount}，last-write-wins

会话缓存在 `CASHEW_SESSION_FILE`（默认 cwd 的 `.cashew-session.json`，0600）；
凭据从 `CASHEW_EMAIL` / `CASHEW_PASSWORD` 环境变量读，不落盘。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEVICE_ID = "spending-mapping"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class CashewHttpError(RuntimeError):
    def __init__(self, code: int, path: str, body: str) -> None:
        self.code = code
        self.path = path
        self.body = body
        super().__init__(f"HTTP {code} {path}: {body[:200]}")


class RebootstrapError(RuntimeError):
    """服务端要求重推全量：cursor 已失效，需从 min_retained_seq 重新拉取。"""

    def __init__(self, min_retained_seq: int) -> None:
        self.min_retained_seq = min_retained_seq
        super().__init__(f"rebootstrap: min_retained_seq={min_retained_seq}")


class CashewClient:
    def __init__(
        self,
        base: str = "http://127.0.0.1:13004",
        *,
        email: str | None = None,
        password: str | None = None,
        session_file: Path | str | None = None,
        device_id: str = DEVICE_ID,
    ) -> None:
        self.base = base.rstrip("/")
        self.email = email or os.environ.get("CASHEW_EMAIL")
        self.password = password or os.environ.get("CASHEW_PASSWORD")
        default_session = Path(os.environ.get("CASHEW_SESSION_FILE") or (Path.cwd() / ".cashew-session.json"))
        self.session_file = Path(session_file) if session_file else default_session
        self.device_id = device_id
        self._token: str | None = None
        self._expires: datetime | None = None
        self.rebootstrapped = False

    # ---------- 登录态 ----------
    def _load_cache(self) -> bool:
        if not self.session_file.exists():
            return False
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if data.get("base") not in (None, self.base):
            return False
        token = data.get("sessionToken")
        expires = _parse_ts(data.get("expiresAt"))
        if not token or not expires or expires - timedelta(days=1) <= _now():
            return False
        self._token, self._expires = token, expires
        return True

    def _save_cache(self, session: dict) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(
            json.dumps(
                {
                    "base": self.base,
                    "email": session.get("user", {}).get("email"),
                    "sessionToken": session.get("sessionToken"),
                    "expiresAt": session.get("expiresAt"),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(self.session_file, 0o600)
        except OSError:
            pass

    def login(self, email: str | None = None, password: str | None = None) -> dict:
        email = email or self.email
        password = password or self.password
        if not email or not password:
            raise RuntimeError("缺少 Cashew 凭据（CASHEW_EMAIL / CASHEW_PASSWORD）")
        session = self._request("POST", "/auth/login", {"email": email, "password": password}, auth=False)
        token = session.get("sessionToken")
        if not token:
            raise RuntimeError("登录响应中没有 sessionToken")
        self._token = token
        self._expires = _parse_ts(session.get("expiresAt"))
        self._save_cache(session)
        return session

    def refresh(self) -> dict:
        if not self._token:
            raise RuntimeError("没有可刷新的会话")
        session = self._request("POST", "/auth/refresh", {"sessionToken": self._token}, auth=False)
        self._token = session.get("sessionToken") or self._token
        self._expires = _parse_ts(session.get("expiresAt")) or self._expires
        return session

    def ensure(self) -> None:
        if self._token and self._expires and self._expires - timedelta(days=1) > _now():
            return
        if self._load_cache():
            return
        self.login()

    # ---------- HTTP ----------
    def _request(self, method: str, path: str, body: dict | None = None, auth: bool = True) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            if not self._token:
                raise RuntimeError("未登录")
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CashewHttpError(exc.code, path, detail) from exc
        return json.loads(raw) if raw else {}

    # ---------- 同步 ----------
    def push(self, changes: list[dict]) -> dict:
        self.ensure()
        return self._request("POST", "/sync/push", {"deviceId": self.device_id, "changes": changes})

    def pull(self, since: int = 0, limit: int = 2000) -> dict:
        self.ensure()
        query = urllib.parse.urlencode({"since": since, "limit": limit})
        try:
            return self._request("GET", f"/sync/pull?{query}")
        except CashewHttpError as exc:
            if exc.code == 409:
                try:
                    payload = json.loads(exc.body)
                    raise RebootstrapError(int(payload.get("minRetainedSeq", 0))) from exc
                except (ValueError, KeyError, json.JSONDecodeError):
                    raise
            raise

    def pull_all(self, since: int = 0, limit: int = 2000) -> tuple[list[dict], int]:
        self.rebootstrapped = False
        cursor = since
        changes: list[dict] = []
        pages = 0
        while True:
            try:
                page = self.pull(cursor, limit)
            except RebootstrapError as exc:
                self.rebootstrapped = True
                cursor = exc.min_retained_seq
                continue
            changes.extend(page.get("changes", []))
            cursor = page.get("nextCursor", cursor)
            pages += 1
            if not page.get("hasMore") or pages > 1000:
                break
        return changes, cursor
