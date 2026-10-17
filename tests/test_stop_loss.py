import tempfile
import unittest

from book.book import OpenPosition
from broker.base import Order
from desk.config import DeskConfig, FeeConfig, Mode
from desk.feed import _TickStore
from desk.run import Desk
from judge.client import JudgeError
from logs.writer import EventLog, read_events
from risk.stops import adverse_move_bps, stop_hit
from state.snapshot import Tick


class DownJudge:
    def __init__(self):
        self.calls = 0

    def ask(self, state, questions, deadline):
        self.calls += 1
        raise JudgeError("judge unreachable")


def feed_with_move(move_bps: float, at: float = 1500.0) -> _TickStore:
    """Mid 100 with a tiny wiggle, then a step move of move_bps at `at`."""
    ticks = []
    for k in range(2000):
        mid = 100.0 * (1 + (0.0001 if k % 2 else 0)) * (1 + move_bps / 1e4 if k >= at else 1)
        ticks.append(Tick(float(k), "X", mid - 0.005, mid + 0.005, 100.0, 100.0))
    return _TickStore({"X": ticks})


def desk_with_position(tmp: str, move_bps: float, side: str = "buy") -> tuple[Desk, DownJudge]:
    cfg = DeskConfig(mode=Mode.PAPER, symbols=("X",), fees=FeeConfig(slippage_bps=0), log_dir=tmp)
    judge = DownJudge()
    desk = Desk(cfg, feed_with_move(move_bps), judge, EventLog(tmp))
    desk.step(1000.0)  # flat book, judge down: nothing happens
    fill = desk.paper.submit(Order("entry", "X", side, 2.0, "market", None, False, 1000.0))
    desk.book.open(OpenPosition("X", side, fill.qty, fill.price, fill.ts, "d-entry"))
    return desk, judge


class StopMathTest(unittest.TestCase):
    def test_adverse_move(self):
        self.assertAlmostEqual(adverse_move_bps("buy", 100, 99), 100)
        self.assertAlmostEqual(adverse_move_bps("sell", 100, 99), -100)
        self.assertIsNone(stop_hit("buy", 100, 99.5, 75))
        self.assertAlmostEqual(stop_hit("sell", 100, 101, 75), 100)


class StopLossDeskTest(unittest.TestCase):
    def test_fires_with_judge_down_and_without_asking_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk, judge = desk_with_position(tmp, move_bps=-100)
            calls = judge.calls
            desk.step(1600.0)
            self.assertIsNone(desk.book.position)
            self.assertEqual(judge.calls, calls)
            self.assertEqual(desk.bench.benched("X", 1600.0).check, "stop_loss")
            ev = list(read_events(tmp))
            (closed,) = [e for e in ev if e["type"] == "trade_closed"]
            self.assertEqual(closed["exit_reason"], "stop_loss")
            self.assertEqual([e for e in ev if e["type"] == "veto"], [])

    def test_holds_inside_the_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk, judge = desk_with_position(tmp, move_bps=-50)
            calls = judge.calls
            desk.step(1600.0)
            self.assertIsNotNone(desk.book.position)
            self.assertEqual(judge.calls, calls + 1)  # asked, failed, held

    def test_short_stops_on_rally(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk, _ = desk_with_position(tmp, move_bps=100, side="sell")
            desk.step(1600.0)
            self.assertIsNone(desk.book.position)

    def test_fires_during_rate_limit_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk, _ = desk_with_position(tmp, move_bps=-100)
            desk.jev_cooldown_until = float("inf")
            desk.step(1600.0)
            self.assertIsNone(desk.book.position)


if __name__ == "__main__":
    unittest.main()
