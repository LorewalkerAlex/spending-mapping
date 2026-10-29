"""同步引擎 + 假服务端 的联合测试（B 层）。

覆盖：LWW 判定、2 分钟夹紧、幂等重推、整行覆盖与排除集、删除补回、
批处理、部分失败重试收敛、409 rebootstrap。
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from spending_mapping.engine import (
    MappedTransaction,
    plan_pushes,
    remote_alive,
    sync_once,
    sync_until_clean,
)
from spending_mapping.records import ImportTransaction
from spending_mapping.timestamps import TimestampSequence
from tests.fake_server import CLAMP_MS, RebootstrapError, FakeCashewServer


def txn(pk: str, amount: str = "-10.00", merchant: str = "盒马", category: str = "日常采购") -> MappedTransaction:
    row = ImportTransaction(
        source_record_id=pk,
        date="2025-08-09",
        merchant=merchant,
        category=category,
        amount=Decimal(amount),
        gross=Decimal("10.00"),
        offset=Decimal("0"),
        note="",
        description=merchant,
    )
    return MappedTransaction(txn=row, category_pk="2")


def pk_of(change: dict) -> str:
    return change["payload"]["transactionPk"]


class FakeServerSemanticsTests(unittest.TestCase):
    def test_new_rows_insert(self):
        server = FakeCashewServer()
        resp = server.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                             "modifiedAt": 1000, "payload": {"transactionPk": "src_a"}}])
        self.assertEqual(resp["conflictCount"], 0)
        self.assertEqual(len(server.alive()), 1)

    def test_newer_timestamp_overwrites(self):
        server = FakeCashewServer()
        server.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                      "modifiedAt": 1000, "payload": {"transactionPk": "src_a", "amount": -1.0}}])
        server.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                      "modifiedAt": 2000, "payload": {"transactionPk": "src_a", "amount": -2.0}}])
        self.assertEqual(server.alive()["src_a"]["payload"]["amount"], -2.0)

    def test_older_timestamp_is_conflict_and_dropped(self):
        server = FakeCashewServer()
        server.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                      "modifiedAt": 2000, "payload": {"transactionPk": "src_a", "amount": -2.0}}])
        resp = server.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                             "modifiedAt": 1000, "payload": {"transactionPk": "src_a", "amount": -9.0}}])
        self.assertEqual(resp["conflictCount"], 1)
        self.assertEqual(server.alive()["src_a"]["payload"]["amount"], -2.0)

    def test_same_timestamp_same_payload_is_noop(self):
        server = FakeCashewServer()
        change = {"table": "Transaction", "pk": "src_a", "deleted": False,
                  "modifiedAt": 1000, "payload": {"transactionPk": "src_a"}}
        server.push([change])
        resp = server.push([dict(change)])
        self.assertEqual(resp["conflictCount"], 0)
        self.assertEqual(len(server.alive()), 1)

    def test_same_timestamp_different_payload_is_conflict(self):
        server = FakeCashewServer()
        server.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                      "modifiedAt": 1000, "payload": {"transactionPk": "src_a", "amount": -1.0}}])
        resp = server.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                             "modifiedAt": 1000, "payload": {"transactionPk": "src_a", "amount": -2.0}}])
        self.assertEqual(resp["conflictCount"], 1)

    def test_future_timestamp_is_clamped(self):
        server = FakeCashewServer()
        far_future = server.server_time_ms + 10 * 60 * 1000
        server.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                      "modifiedAt": far_future, "payload": {"transactionPk": "src_a"}}])
        self.assertEqual(server.alive()["src_a"]["modifiedAt"], server.server_time_ms + CLAMP_MS)

    def test_pull_paginates_by_seq(self):
        server = FakeCashewServer()
        for i in range(5):
            server.push([{"table": "Transaction", "pk": f"src_{i}", "deleted": False,
                          "modifiedAt": 1000 + i, "payload": {"transactionPk": f"src_{i}"}}])
        page = server.pull(since=0, limit=3)
        self.assertEqual(len(page["changes"]), 3)
        self.assertTrue(page["hasMore"])
        page2 = server.pull(since=page["nextCursor"], limit=3)
        self.assertEqual(len(page2["changes"]), 2)
        self.assertFalse(page2["hasMore"])

    def test_reset_then_pull_below_floor_rebootstraps(self):
        server = FakeCashewServer()
        server.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                      "modifiedAt": 1000, "payload": {"transactionPk": "src_a"}}])
        server.reset()
        with self.assertRaises(RebootstrapError):
            server.pull(since=0)


class EngineTests(unittest.TestCase):
    def test_new_items_planned(self):
        server = FakeCashewServer()
        items = [txn("src_a"), txn("src_b")]
        plan = plan_pushes(items, remote_alive(server.pull_all()), exclude=set(), seq=TimestampSequence())
        self.assertEqual({pk_of(c) for c in plan}, {"src_a", "src_b"})

    def test_unchanged_skipped_and_changed_pushed(self):
        server = FakeCashewServer()
        items = [txn("src_a")]
        seq = TimestampSequence()
        sync_once(server, items, exclude=set(), seq=seq)

        # 内容不变 → 不再推
        again = sync_once(server, items, exclude=set(), seq=seq)
        self.assertEqual(again.planned, 0)

        # 改了金额 → 推一笔（复用同一序列，时间戳递增，不会被判同毫秒冲突）
        changed = [txn("src_a", amount="-20.00")]
        result = sync_once(server, changed, exclude=set(), seq=seq)
        self.assertEqual(result.planned, 1)
        self.assertEqual(server.alive()["src_a"]["payload"]["amount"], -20.0)

    def test_excluded_not_overwritten(self):
        server = FakeCashewServer()
        seq = TimestampSequence()
        sync_once(server, [txn("src_a")], exclude=set(), seq=seq)
        # 用户在 cashew 改了金额（不在我们的应然里），且 src_a 在排除集内 → 不覆盖
        server.push([{"table": "Transaction", "pk": "src_a", "deleted": False,
                      "modifiedAt": server.server_time_ms, "payload": {"transactionPk": "src_a", "amount": -99.0}}])
        result = sync_once(server, [txn("src_a")], exclude={"src_a"}, seq=seq)
        self.assertEqual(result.planned, 0)
        self.assertEqual(server.alive()["src_a"]["payload"]["amount"], -99.0)

    def test_remote_deletion_is_recreated(self):
        server = FakeCashewServer(server_time_ms=10_000)
        clock = iter(range(1000, 10_000)).__next__
        seq = TimestampSequence(clock=clock)
        sync_once(server, [txn("src_a")], exclude=set(), seq=seq)

        # 远端被删（比我们当前时间戳更新）→ 应然还在 → 推回。
        # 推回的前提：序列必须起播到"服务端最大 modifiedAt + 1"以上。
        server.push([{"table": "Transaction", "pk": "src_a", "deleted": True,
                      "modifiedAt": 5000, "payload": {}}])
        self.assertNotIn("src_a", server.alive())

        resumed = TimestampSequence(start_ms=5000, clock=clock)
        result = sync_once(server, [txn("src_a")], exclude=set(), seq=resumed)
        self.assertEqual(result.planned, 1)
        self.assertIn("src_a", server.alive())

    def test_review_rename_then_repush_flow(self):
        server = FakeCashewServer()
        items = [txn("src_a")]
        seq = TimestampSequence()
        sync_once(server, items, exclude=set(), seq=seq)
        # 审核改名：应然里商户名变了 → 重推
        renamed = [txn("src_a", merchant="盒马鲜生")]
        result = sync_once(server, renamed, exclude=set(), seq=seq)
        self.assertEqual(result.planned, 1)
        self.assertEqual(server.alive()["src_a"]["payload"]["name"], "盒马鲜生")

    def test_partial_failure_converges(self):
        server = FakeCashewServer()
        server.fail_next_push = 1  # 第一次 push 失败
        items = [txn(f"src_{i:03d}") for i in range(10)]
        seq = TimestampSequence()
        # sync_once 单次会失败抛异常；用 sync_until_clean 兜底重试
        try:
            sync_once(server, items, exclude=set(), seq=seq)
        except RuntimeError:
            pass
        total = sync_until_clean(server, items, exclude=set(), seq=seq)
        self.assertEqual(len(server.alive()), 10)
        self.assertLessEqual(total.rounds, 3)

    def test_batch_size_is_respected(self):
        from spending_mapping.engine import push_batches
        changes = [{"pk": i} for i in range(450)]
        batches = push_batches(changes, batch_size=200)
        self.assertEqual([len(b) for b in batches], [200, 200, 50])


if __name__ == "__main__":
    unittest.main()
