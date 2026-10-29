"""常驻服务：定时从邮箱拉取账单邮件，按 sha256 落盘。

Docker 里没有 systemd timer，所以是"长驻进程 + 内部轮询"。启动先拉一次，
之后每 EMAIL_POLL_INTERVAL_SECONDS 秒一轮。单轮失败只记日志，不退出。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import MailPullConfig
from .mail_pull import pull_cmb

logger = logging.getLogger("spending-mapping")


def _run_once(cfg: MailPullConfig) -> None:
    result = pull_cmb(
        host=cfg.host,
        port=cfg.port,
        address=cfg.address,
        auth_code=cfg.auth_code,
        mailbox=cfg.mailbox,
        subject_keyword=cfg.subject_keyword,
        since=cfg.since,
        evidence_dir=Path(cfg.evidence_dir),
    )
    if result.errors:
        logger.error("拉取完成但有错误：fetched=%d added=%d dup=%d errors=%s",
                     result.fetched, result.added, result.duplicates, result.errors)
    else:
        logger.info("拉取完成：fetched=%d added=%d duplicates=%d",
                    result.fetched, result.added, result.duplicates)


def run_forever(cfg: MailPullConfig) -> int:
    logger.info("启动，邮箱=%s 关键词=%r 间隔=%ds",
                cfg.mailbox, cfg.subject_keyword, cfg.interval_seconds)
    try:
        while True:
            try:
                _run_once(cfg)
            except Exception:  # noqa: BLE001 — 单轮失败不退出
                logger.exception("本轮拉取失败")
            try:
                time.sleep(cfg.interval_seconds)
            except KeyboardInterrupt:
                break
    except KeyboardInterrupt:
        pass
    logger.info("退出")
    return 0
