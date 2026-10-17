import tempfile
import unittest

from desk.coinbase_feed import CoinbaseFeed, RateLimited, candles_to_ticks, parse_book, parse_time
from desk.config import DeskConfig, Mode
from desk.feed import _TickStore
from desk.run import Desk, run_live
from judge.fake import FakeJudge
from logs.writer import EventLog, read_events
from state.snapshot import Tick


def book(t: str, bid="100.0", ask="100.1", seq=1):
    return {"bids": [[bid, "2.5", 3]], "asks": [[ask, "1.5", 4]], "sequence": seq, "time": t}


class ParseTest(unittest.TestCase):
    def test_parse_time_nanoseconds(self):
        self.assertAlmostEqual(parse_time("2026-10-17T22:48:17.780493982Z"), 1792277297.780494, places=5)
        self.assertEqual(parse_time("2026-10-17T22:48:17Z"), 1792277297.0)

    def test_parse_book(self):
        t = parse_book("BTC-USD", book("2026-10-17T22:48:17.5Z"))
        self.assertEqual((t.bid, t.ask, t.bid_size, t.ask_size), (100.0, 100.1, 2.5, 1.5))
        self.assertIsNone(parse_book("X", {"bids": [], "asks": [["1", "1", 1]], "time": "2026-10-17T00:00:00Z"}))
        self.assertIsNone(parse_book("X", book("2026-10-17T00:00:00Z", bid="101", ask="100")))  # crossed


class FeedTest(unittest.TestCase):
    def test_dedupes_cached_repeats_and_keeps_order(self):
        responses = iter([book("2026-10-17T00:00:01Z", seq=1), book("2026-10-17T00:00:01Z", seq=1),
                          book("2026-10-17T00:00:03Z", seq=2), book("2026-10-17T00:00:02Z", seq=3)])
        feed = CoinbaseFeed(("X",), history_s=60, fetch=lambda s: next(responses))
        for _ in range(4):
            feed.poll_once()
        ts = [t.ts for t in feed.ticks("X", 1e12, 1e12)]
        self.assertEqual(len(ts), 2)  # repeat dropped, out-of-order dropped
        self.assertLess(ts[0], ts[1])

    def test_prunes_old_history(self):
        seq = iter(range(100))
        times = iter(f"2026-10-17T00:{m:02d}:00Z" for m in range(10))
        feed = CoinbaseFeed(("X",), history_s=120, fetch=lambda s: book(next(times), seq=next(seq)))
        for _ in range(10):
            feed.poll_once()
        self.assertEqual(len(feed.ticks("X", 1e12, 1e12)), 3)  # minutes 7, 8, 9

    def test_rate_limit_backs_off_and_errors_do_not_raise(self):
        errors = []

        def fetch(symbol):
            if symbol == "A":
                raise RateLimited(symbol)
            raise OSError("network down")

        feed = CoinbaseFeed(("A",), history_s=60, fetch=fetch, on_error=lambda s, d: errors.append(d))
        feed.poll_once()
        feed.poll_once()
        self.assertEqual(feed.backoff_s, 2.0)
        feed2 = CoinbaseFeed(("B",), history_s=60, fetch=fetch, on_error=lambda s, d: errors.append(d))
        feed2.poll_once()
        self.assertTrue(any("network down" in e for e in errors))

    def test_refuses_too_many_requests(self):
        with self.assertRaises(ValueError):
            CoinbaseFeed(tuple("ABCDEF"), history_s=60, poll_interval_s=1.0)


class SparseTicksTest(unittest.TestCase):
    def test_15m_return_found_with_2s_ticks(self):
        ticks = [Tick(float(k), "X", 99.95, 100.05, 50, 50) for k in range(0, 2000, 2)]
        cfg = DeskConfig(mode=Mode.SHADOW, symbols=("X",))
        with tempfile.TemporaryDirectory() as tmp:
            desk = Desk(cfg, _TickStore({"X": ticks}), FakeJudge(), EventLog(tmp))
            for now in (1001.0, 1002.0, 1003.5):
                snap = desk._snapshot("X", now, 10_000)
                self.assertIsNotNone(snap.returns_bps["15m"], now)


def candles(end_minute: int, n: int, price: float = 100.0, step: float = 0.05) -> list[list[float]]:
    """n 1-minute candles, newest first, the newest starting at end_minute*60 (Coinbase order)."""
    out = []
    for k in range(n):
        start = (end_minute - k) * 60
        close = price + step * (n - k)
        out.append([start, close - 1, close + 1, close, close, 1.0])
    return out


class BackfillTest(unittest.TestCase):
    def test_candles_to_ticks_drops_unfinished_candle(self):
        ticks = candles_to_ticks("X", candles(end_minute=10, n=3), end=600.0)  # candle 600-660 is unfinished
        self.assertEqual([t.ts for t in ticks], [540.0, 600.0])
        self.assertEqual((ticks[0].bid, ticks[0].ask, ticks[0].bid_size), (ticks[0].ask, ticks[0].bid, 0.0))

    def test_requests_end_on_completed_minute(self):
        calls = []

        def hist(symbol, start, end):
            calls.append((start, end))
            return candles(end_minute=int(end // 60), n=18)

        feed = CoinbaseFeed(("X",), history_s=1020, fetch=lambda s: {}, fetch_history=hist)
        added = feed.backfill(now=60_036.0)
        start, end = calls[0]
        self.assertEqual(end, 60_000.0)
        self.assertLessEqual(start, end - 1020)
        self.assertEqual(added["X"], 17)  # newest candle (starting at end) is unfinished, dropped
        self.assertLessEqual(max(t.ts for t in feed.ticks("X", 1e12, 1e12)), end)

    def test_failure_is_reported_not_raised(self):
        errors = []

        def boom(symbol, start, end):
            raise OSError("down")

        feed = CoinbaseFeed(("X",), history_s=1020, fetch=lambda s: {}, fetch_history=boom, on_error=lambda s, d: errors.append(d))
        self.assertEqual(feed.backfill(now=60_000.0), {"X": 0})
        self.assertIn("warming up live", errors[0])

    def test_never_overlaps_live_ticks(self):
        live = iter([book("1970-01-01T16:39:50Z", seq=1)])  # ts 59_990
        feed = CoinbaseFeed(("X",), history_s=1020, fetch=lambda s: next(live), fetch_history=lambda s, a, b: candles(int(b // 60), 18))
        feed.poll_once()
        feed.backfill(now=60_036.0)
        ts = [t.ts for t in feed.ticks("X", 1e12, 1e12)]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(t < 59_990 for t in ts[:-1]))

    def test_desk_uses_backfill_but_will_not_trade_on_it_alone(self):
        live = iter([book("1970-01-01T16:40:05Z", seq=1)])  # ts 60_005, a fresh book after backfill
        feed = CoinbaseFeed(("BTC-USD",), history_s=1020, fetch=lambda s: next(live), fetch_history=lambda s, a, b: candles(int(b // 60), 18))
        feed.backfill(now=60_001.0)
        cfg = DeskConfig(mode=Mode.SHADOW, symbols=("BTC-USD",))
        with tempfile.TemporaryDirectory() as tmp:
            desk = Desk(cfg, feed, FakeJudge(), EventLog(tmp))
            desk.step(60_002.0)  # only candle history so far
            feed.poll_once()
            desk.step(60_010.0)  # now a live tick is the newest
            checks = [e["check"] for e in read_events(tmp) if e["type"] == "rejection"]
            self.assertEqual(checks[0], "no_book")
            self.assertNotIn("insufficient_history", checks)
            snap = desk._snapshot("BTC-USD", 60_010.0, 10_000)
            self.assertIsNotNone(snap.returns_bps["15m"])
            self.assertGreater(snap.spread_bps, 0)
            self.assertTrue(snap.has_book)


class RunLiveTest(unittest.TestCase):
    def test_steps_on_block_boundaries_and_survives_a_bad_block(self):
        clock = {"t": 1005.0}
        stepped = []

        class FlakyDesk:
            cfg = DeskConfig()

            def step(self, now):
                stepped.append(now)
                if len(stepped) == 1:
                    raise RuntimeError("boom")

        def wait(s):
            clock["t"] += s

        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog(tmp)
            self.assertEqual(run_live(FlakyDesk(), 3, log, clock=lambda: clock["t"], wait=wait), 3)
            self.assertEqual(stepped, [1010.0, 1020.0, 1030.0])
            (err,) = [e for e in read_events(tmp) if e["type"] == "error"]
            self.assertEqual(err["check"], "desk_step")


if __name__ == "__main__":
    unittest.main()
