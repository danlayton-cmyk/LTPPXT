import tempfile
import unittest

from desk.coinbase_feed import CoinbaseFeed, RateLimited, parse_book, parse_time
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
