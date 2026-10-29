"""推送时间戳序列。"""

from __future__ import annotations

import unittest

from spending_mapping.timestamps import MAX_CLOCK_AHEAD_MS, TimestampSequence, clamp_ahead, now_ms


class SequenceTests(unittest.TestCase):
    def test_strictly_increasing_over_many_calls(self):
        seq = TimestampSequence()
        values = seq.take(5000)
        self.assertEqual(len(values), 5000)
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(set(values)), 5000)

    def test_never_goes_backwards_even_when_clock_is_behind(self):
        # 用"未来"作为起点，模拟时钟回拨：序列必须继续递增而不是跟随系统时间
        future = now_ms() + 3_600_000
        seq = TimestampSequence(start_ms=future)
        values = seq.take(3)
        self.assertTrue(all(v > future for v in values))
        self.assertEqual(values, sorted(values))

    def test_take_zero(self):
        self.assertEqual(TimestampSequence().take(0), [])


class ClampTests(unittest.TestCase):
    def test_within_skew_is_kept(self):
        server = 1_793_000_000_000
        self.assertEqual(clamp_ahead(server + 1000, server), server + 1000)

    def test_beyond_two_minutes_is_clamped(self):
        server = 1_793_000_000_000
        clamped = clamp_ahead(server + MAX_CLOCK_AHEAD_MS + 5000, server)
        self.assertEqual(clamped, server + MAX_CLOCK_AHEAD_MS)


if __name__ == "__main__":
    unittest.main()
