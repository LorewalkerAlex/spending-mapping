"""命令行：import / review / apply-category / apply-refund / reset。

默认无参数时 `python -m spending_mapping` 跑常驻拉取服务（容器行为不变）。

安全边界：
  - 写操作默认 dry-run，必须 `--apply` 才落库
  - `reset` 只删主键 `src_` 开头的交易（我们导入的），用户手记的 UUID 交易不动
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .cashew_client import CashewClient
from .engine import push_batches, remote_alive
from .ledger import ImportLedger
from .parser import parse_directory
from .pipeline import import_transactions
from .review import (
    IMPORTED_PREFIX,
    apply_category,
    apply_refund_income,
    apply_refund_offset,
    list_pending_categories,
    list_pending_refunds,
)
from .review_log import ReviewLog
from .timestamps import TimestampSequence

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


def make_ledger() -> ImportLedger:
    return ImportLedger.load(data_dir() / "imported.jsonl")


def make_log() -> ReviewLog:
    return ReviewLog.load(data_dir() / "review-log.jsonl")


def cmd_import(args: argparse.Namespace) -> int:
    statements = parse_directory(evidence_dir())
    client = make_client()
    client.ensure()
    result = import_transactions(
        client,
        statements,
        make_ledger(),
        apply=args.apply,
        limit=args.limit,
        batch_size=args.batch_size,
    )
    print(
        f"净额 {result.net_rows} 笔；台账已入账 {result.skipped_by_ledger} 笔；"
        f"远端已有 {result.skipped_remote} 笔"
    )
    print(f"本次待推 {result.planned} 笔；未抵消退款 {result.unmatched} 笔 / {result.unmatched_total}")
    if not args.apply:
        print("dry-run：未写入。确认后加 --apply")
        return 0
    print(f"已推送 {result.pushed} 笔，冲突 {result.conflicts}")
    return 0 if result.conflicts == 0 else 2


def cmd_review(args: argparse.Namespace) -> int:
    client = make_client()
    client.ensure()
    categories = list_pending_categories(client, make_log())
    refunds = list_pending_refunds(parse_directory(evidence_dir()), make_log())

    limit = args.limit or 20
    print(f"待审分类：{len(categories)} 个商户")
    for item in categories[:limit]:
        print(f"  {item.name:<24} {item.count:>3} 笔  {item.total:>12}  {item.sample_note[:30]}")
    print(f"待审退款：{len(refunds)} 笔")
    for item in refunds[:limit]:
        print(f"  {item.date}  {item.amount:>9}  {item.merchant}  ({item.record_id})")
    return 0


def cmd_apply_category(args: argparse.Namespace) -> int:
    client = make_client()
    client.ensure()
    changed = apply_category(
        client, args.name, args.category, make_log(),
        apply=args.apply, batch_size=args.batch_size,
    )
    print(f"商户「{args.name}」→ 分类 {args.category}：{'已改' if args.apply else '将改'} {changed} 笔")
    if not args.apply:
        print("dry-run：未写入。确认后加 --apply")
    return 0


def cmd_apply_refund(args: argparse.Namespace) -> int:
    client = make_client()
    client.ensure()
    statements = parse_directory(evidence_dir())
    log = make_log()

    if bool(args.offset) == bool(args.income):
        print("必须且只能指定 --offset <目标交易pk> 或 --income")
        return 1

    if args.offset:
        apply_refund_offset(client, statements, args.record, args.offset, log, apply=args.apply)
        print(f"退款 {args.record} 抵消 {args.offset}：{'已写入' if args.apply else '将写入'}")
    else:
        if not args.category:
            print("--income 需要 --category")
            return 1
        apply_refund_income(client, statements, args.record, args.category, log, apply=args.apply)
        print(f"退款 {args.record} 记为收入，分类 {args.category}：{'已写入' if args.apply else '将写入'}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    client = make_client()
    client.ensure()
    ledger = make_ledger()
    changes, _ = client.pull_all()
    alive = remote_alive(changes)
    ours = [change for pk, change in alive.items() if pk.startswith(IMPORTED_PREFIX)]

    print(f"远端共 {len(alive)} 笔；其中我们导入的 {len(ours)} 笔；台账记录 {len(ledger)} 笔")
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
    for change in ours:
        ledger.mark_deleted(change["pk"])

    after = remote_alive(client.pull_all()[0])
    left = [pk for pk in after if pk.startswith(IMPORTED_PREFIX)]
    print(f"已删除 {len(ours)} 笔；剩余我们导入的 {len(left)} 笔；用户手记 {len(after) - len(left)} 笔")
    return 0 if not left else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spending-mapping", description="账单 → Cashew 导入与审核")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="解析账单并导入 Cashew（默认 dry-run）")
    imp.add_argument("--apply", action="store_true", help="真正写入")
    imp.add_argument("--limit", type=int, default=0, help="只处理前 N 笔")
    imp.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    imp.set_defaults(func=cmd_import)

    rev = sub.add_parser("review", help="列待审分类与待审退款")
    rev.add_argument("--limit", type=int, default=20)
    rev.set_defaults(func=cmd_review)

    cat = sub.add_parser("apply-category", help="把某商户名下的交易改成指定分类")
    cat.add_argument("--name", required=True)
    cat.add_argument("--category", required=True)
    cat.add_argument("--apply", action="store_true")
    cat.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    cat.set_defaults(func=cmd_apply_category)

    ref = sub.add_parser("apply-refund", help="处理一笔未匹配退款")
    ref.add_argument("--record", required=True, help="退款记录的 record_id")
    ref.add_argument("--offset", help="抵消到这笔交易（目标 pk）")
    ref.add_argument("--income", action="store_true", help="记为收入")
    ref.add_argument("--category", help="收入所用分类 pk")
    ref.add_argument("--apply", action="store_true")
    ref.set_defaults(func=cmd_apply_refund)

    res = sub.add_parser("reset", help="删除我们导入的交易（src_ 前缀）")
    res.add_argument("--yes", action="store_true")
    res.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    res.set_defaults(func=cmd_reset)

    return parser


def dispatch(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
