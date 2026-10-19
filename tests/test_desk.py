import tempfile
import unittest
from collections import Counter
from dataclasses import replace

from desk.config import DeskConfig, FeeConfig, Mode
from desk.feed import SyntheticFeed
from desk.run import Desk
from judge.client import JudgeDeadline
from logs.writer import EventLog, read_events
from reports.calibration import brier, collect_pairs, render
from reports.daily import summarize
from tests.helpers import BULLISH_GAMBLER, StubJudge, noul

SECRET = "desk-secret-value-that-must-never-be-logged"


def make_desk(mode: Mode, judge, tmp: str, **cfg_over):
    cfg = replace(DeskConfig(mode=mode, bot="gambler", symbols=("BTC-USD", "ETH-USD"), fees=FeeConfig(taker_fee_bps=1, slippage_bps=0)), log_dir=tmp, **cfg_over)
    feed = SyntheticFeed(cfg.symbols, start_ts=1_760_000_000, seconds=3_000, seed=3)
    desk = Desk(cfg, feed, judge, EventLog(tmp, secrets=[SECRET]))
    start = feed.first_ts() + cfg.state.vol_window_s + 1
    return desk, start


class DeskTest(unittest.TestCase):
    def test_shadow_never_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk, start = make_desk(Mode.SHADOW, StubJudge(BULLISH_GAMBLER), tmp)
            for i in range(40):
                desk.step(start + i * 10)
            ev = list(read_events(tmp))
            orders = [e for e in ev if e["type"] == "order"]
            self.assertTrue(orders, "shadow should log what it would have done")
            self.assertTrue(all(not o["executed"] for o in orders))
            self.assertFalse(any(e["type"] == "fill" for e in ev))
            self.assertIsNone(desk.paper)

    def test_paper_round_trip_and_no_scan_while_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            judge = StubJudge(BULLISH_GAMBLER)
            desk, start = make_desk(Mode.PAPER, judge, tmp)
            desk.step(start)
            self.assertIsNotNone(desk.book.position)
            calls_after_entry = judge.calls
            desk.step(start + 10)  # position open: one exit question, no scan
            self.assertEqual(judge.calls, calls_after_entry + 1)
            judge.answers = {**BULLISH_GAMBLER, "exit_now": noul(0.99)}
            desk.step(start + 20)
            self.assertIsNone(desk.book.position)
            ev = list(read_events(tmp))
            types = Counter(e["type"] for e in ev)
            self.assertEqual(types["fill"], 2)
            self.assertEqual(types["trade_closed"], 1)
            self.assertTrue(all(e["model"] == "jev-1.13.0" for e in ev if e["type"] == "answer"))

    def test_wrong_model_version_is_vetoed(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk, start = make_desk(Mode.PAPER, StubJudge(BULLISH_GAMBLER, model="jev-1.14.0"), tmp)
            desk.step(start)
            self.assertIsNone(desk.book.position)
            vetoes = [e for e in read_events(tmp) if e["type"] == "veto"]
            self.assertEqual({v["check"] for v in vetoes}, {"model_version"})

    def test_judge_timeout_holds(self):
        class SlowJudge:
            def ask(self, state, questions, deadline):
                raise JudgeDeadline("too slow")

        with tempfile.TemporaryDirectory() as tmp:
            desk, start = make_desk(Mode.PAPER, SlowJudge(), tmp)
            desk.step(start)
            self.assertIsNone(desk.book.position)
            ev = list(read_events(tmp))
            self.assertFalse(any(e["type"] == "order" for e in ev))
            self.assertTrue(any(e["type"] == "rejection" and e["check"] == "jev_deadline" for e in ev))

    def test_every_rejection_names_its_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk, start = make_desk(Mode.SHADOW, StubJudge(BULLISH_GAMBLER), tmp, funnel=replace(DeskConfig().funnel, max_spread_bps=0.1))
            desk.step(start)
            rej = [e for e in read_events(tmp) if e["type"] == "rejection"]
            self.assertTrue(rej)
            self.assertTrue(all(e["stage"] == "free_cut" and e["check"] == "spread_too_wide" for e in rej))

    def test_outcomes_reports_and_no_secrets_in_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 5-minute horizon so outcomes come due inside the 800 s this test simulates.
            desk, start = make_desk(Mode.PAPER, StubJudge(BULLISH_GAMBLER), tmp, outcome_horizon_s=300.0)
            for i in range(80):
                desk.step(start + i * 10)
            ev = list(read_events(tmp))
            self.assertTrue(any(e["type"] == "outcome" for e in ev))
            pairs = collect_pairs(ev)
            self.assertTrue(pairs)
            self.assertIsNotNone(brier(pairs))
            md, svg = render(pairs)
            self.assertIn("jev-1.13.0", md)
            self.assertTrue(svg.startswith("<svg"))
            self.assertIn("Risk vetoes", summarize(ev))
            for path in __import__("pathlib").Path(tmp).glob("*.jsonl"):
                self.assertNotIn(SECRET, path.read_text())

    def test_outcomes_wait_for_the_bots_holding_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk, start = make_desk(Mode.SHADOW, StubJudge(BULLISH_GAMBLER), tmp)  # gambler: 1800 s
            for i in range(30):
                desk.step(start + i * 10)
            self.assertFalse(any(e["type"] == "outcome" for e in read_events(tmp)))
            due = [d for d, _, _ in desk.pending_outcomes]
            self.assertEqual(min(due), start + 1800)

    def test_log_scrubs_secret_keys_and_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog(tmp, secrets=[SECRET])
            log.write("error", 0, api_key="abc", detail=f"auth {SECRET} failed", usage={"input_tokens": 3})
            (e,) = read_events(tmp)
            self.assertEqual(e["api_key"], "[redacted]")
            self.assertNotIn(SECRET, e["detail"])
            self.assertEqual(e["usage"]["input_tokens"], 3)


if __name__ == "__main__":
    unittest.main()
