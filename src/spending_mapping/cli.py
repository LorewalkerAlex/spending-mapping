"""命令行子命令：sync（导入）与 reset（删除我们导入的交易）。

默认无参数时 `python -m spending_mapping` 仍跑常驻拉取服务（容器行为不变）。

安全边界：
  - `reset` 只删主键以 `src_` 开头的交易（我们导入的），用户手记的 UUID 交易不受影响
  - `sync` 默认 dry-run，必须显式 `--apply` 才写
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

from .cashew_client import CashewClient
from .categories import load_categories
from .engine import plan_pushes, push_batches, remote_alive
from .mapping import MappingStore
from .parser import parse_directory
from .pipeline import build_items
from .timestamps import TimestampSequence

IMPORTED_PREFIX = "src_"
DEFAULT_BATCH = 200


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def evidence_dir() -> Path:
    override = os.environ.get("EVIDENCE_DIR")
    return Path(override) if override else data_dir() / "evidence" / "cmb-email"


def make_client() -> CashewClient:
    return CashewClient(
        os.environ.get("CASHEW_BASE", "http://127.0.0.1:13004"),
        session_file=data_dir() / ".cashew-session.json",
    )


def make_mapping() -> MappingStore:
    return MappingStore.load(load_categories(), data_dir() / "review-log.jsonl")


def cmd_sync(args: argparse.Namespace) -> int:
    statements = parse_directory(evidence_dir())
    items, netted = build_items(statements, make_mapping())
    total_rows = len(items)
    if args.limit:
        items = items[: args.limit]

    client = make_client()
    client.ensure()
    remote = remote_alive(client.pull_all()[0])
    plan = plan_pushes(items, remote, exclude=set(), seq=TimestampSequence())

    unmatched_total = -sum((Decimal(r.amount) for r in netted.discarded), Decimal(0))
    print(f"净额 {total_rows} 笔；本次处理 {len(items)} 笔；"
          f"未抵消 {len(netted.discarded)} 笔 / {unmatched_total}")
    print(f"远端已有 {len(remote)} 笔；本次待推 {len(plan)} 笔")

    if args.dry_run or not plan:
        if args.dry_run:
            print("dry-run：未写入。确认后加 --apply")
        return 0

    pushed = conflicts = 0
    for batch in push_batches(plan, args.batch_size):
        response = client.push(batch)
        pushed += len(batch)
        conflicts += int(response.get("conflictCount", 0))
    print(f"已推送 {pushed} 笔，冲突 {conflicts}")

    after = remote_alive(client.pull_all()[0])
    ours = [pk for pk in after if pk.startswith(IMPORTED_PREFIX)]
    print(f"回读：远端现有 {len(after)} 笔，其中我们导入的 {len(ours)} 笔")
    return 0 if conflicts == 0 else 2


def cmd_reset(args: argparse.Namespace) -> int:
    client = make_client()
    client.ensure()
    changes, _ = client.pull_all()
    alive = remote_alive(changes)
    ours = [change for pk, change in alive.items() if pk.startswith(IMPORTED_PREFIX)]

    print(f"远端共 {len(alive)} 笔；其中我们导入的（{IMPORTED_PREFIX} 前缀）{len(ours)} 笔")
    if not ours:
        print("没有需要删除的交易")
        return 0
    if not args.yes:
        print("不执行删除。确认后加 --yes")
        return 1

    seq = TimestampSequence()
    tombstones = [dict(change, deleted=True, modifiedAt=seq.next()) for change in ours]
    for batch in push_batches(tombstones, args.batch_size):
        client.push(batch)

    after = remote_alive(client.pull_all()[0])
    left = [pk for pk in after if pk.startswith(IMPORTED_PREFIX)]
    print(f"已删除 {len(ours)} 笔；回读剩余我们导入的交易 {len(left)} 笔，"
          f"用户手记交易 {len(after) - len(left)} 笔")
    return 0 if not left else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spending-mapping", description="账单 → Cashew 导入服务")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="解析账单并导入 Cashew（默认 dry-run）")
    sync.add_argument("--dry-run", action="store_true", default=True, help="只统计不写入（默认）")
    sync.add_argument("--apply", dest="dry_run", action="store_false", help="真正写入")
    sync.add_argument("--limit", type=int, default=0, help="只处理前 N 笔（试点用）")
    sync.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    sync.set_defaults(func=cmd_sync)

    reset = sub.add_parser("reset", help="删除我们导入的交易（src_ 前缀）")
    reset.add_argument("--yes", action="store_true", help="确认执行删除")
    reset.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    reset.set_defaults(func=cmd_reset)

    return parser


def dispatch(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
