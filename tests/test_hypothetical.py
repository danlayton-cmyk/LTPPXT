import json
import tempfile
import unittest
from collections import Counter

from desk.config import DeskConfig, FeeConfig, Mode
from desk.feed import _TickStore
from desk.run import Desk
from logs.writer import EventLog, read_events, utc_day
from reports.daily import summarize
from state.snapshot import Tick
from tests.helpers import BULLISH_GAMBLER, StubJudge, choice, noul, score

BULLISH_HOUSE = {"toxic_flow": noul(0.05), "quote_lean": choice({"bid": 0.95, "ask": 0.02, "neither": 0.02, "other": 0.01}),
                 "liquidity_calm": score([0, 0, 0.1, 0.9]), "inventory_adverse": noul(0.2)}
FEES = FeeConfig(taker_fee_bps=5, maker_fee_bps=1, gas_usd_per_fill=0.05, slippage_bps=1)


def feed(move_bps: float = 0.0, at: float = 10_000, spread: float = 0.01) -> _TickStore:
    """Mid 100 with a 2 bps wiggle (about 60 bps of realized vol), then a step move of move_bps at `at`."""
    ticks = []
    for k in range(2000):
        mid = 100.0 * (1.0002 if k % 2 else 1.0) * (1 + move_bps / 1e4 if k >= at else 1)
        ticks.append(Tick(float(k), "X", mid - spread / 2, mid + spread / 2, 100.0, 100.0))
    return _TickStore({"X": ticks})


def make_desk(tmp: str, bot: str = "gambler", mode: Mode = Mode.SHADOW, answers=BULLISH_GAMBLER, **feed_over) -> Desk:
    cfg = DeskConfig(mode=mode, bot=bot, symbols=("X",), fees=FEES, outcome_horizon_s=300.0, log_dir=tmp)
    return Desk(cfg, feed(**feed_over), StubJudge(answers), EventLog(tmp))


def run(desk: Desk, start: float = 1000.0, blocks: int = 1) -> list[dict]:
    for i in range(blocks):
        desk.step(start + i * 10)
    return list(read_events(desk.cfg.log_dir))


def of(events: list[dict], kind: str, **match) -> list[dict]:
    return [e for e in events if e["type"] == kind and all(e.get(k) == v for k, v in match.items())]


class LedgerTest(unittest.TestCase):
    def test_market_entry_closes_at_holding_period_after_fees(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk = make_desk(tmp)
            ev = run(desk, blocks=32)  # 1000 .. 1310
            (closed,) = of(ev, "hypothetical_trade_closed")
            self.assertEqual(closed["exit_reason"], "holding_period")
            self.assertEqual(closed["held_s"], 300)
            qty = 250 / 100.02  # the order is sized off the snapshot mid, from tick 999
            entry, exit_ = 100.005 * 1.0001, 99.995 * 0.9999  # tick 1000 ask and tick 1300 bid, through 1 bps of slippage
            fees = (entry + exit_) * qty * 5 / 1e4 + 2 * 0.05
            self.assertAlmostEqual(closed["entry_price"], entry)
            self.assertAlmostEqual(closed["exit_price"], exit_)
            self.assertAlmostEqual(closed["gross_pnl_usd"], (exit_ - entry) * qty, places=5)
            self.assertAlmostEqual(closed["costs_usd"], fees, places=5)
            self.assertAlmostEqual(closed["net_pnl_usd"], (exit_ - entry) * qty - fees, places=5)
            self.assertLess(closed["net_pnl_usd"], 0)  # a flat market loses the spread, slippage and fees
            # Shadow re-asks every block: one trade at a time, the rest are skipped, and the block
            # that closed the trade takes no new entry. 1310 starts flat and takes one.
            entries = Counter(e["status"] for e in of(ev, "hypothetical_order", role="entry"))
            self.assertEqual(entries, {"filled": 2, "skipped": 30})
            self.assertEqual({e["reason"] for e in of(ev, "hypothetical_order", status="skipped")}, {"position_open"})
            self.assertEqual(of(ev, "hypothetical_order", status="filled", role="entry")[-1]["ts"], 1310)
            # Still shadow: nothing real executes, and no ordinary fill or trade is logged.
            self.assertIsNone(desk.paper)
            self.assertTrue(all(not o["executed"] for o in of(ev, "order")))
            self.assertEqual(of(ev, "fill") + of(ev, "trade_closed") + of(ev, "hypothetical_error"), [])

    def test_stop_loss_closes_and_sits_the_symbol_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk = make_desk(tmp, move_bps=-100, at=1100)
            ev = run(desk, blocks=13)  # the drop shows in the 1110 snapshot
            (closed,) = of(ev, "hypothetical_trade_closed")
            self.assertEqual(closed["exit_reason"], "stop_loss")
            self.assertGreaterEqual(closed["adverse_bps"], 75)
            self.assertEqual(closed["ts"], 1110)
            self.assertEqual(desk.hypothetical.bench.benched("X", 1120).check, "stop_loss")
            self.assertIsNone(desk.bench.benched("X", 1120))  # the desk's own bench is untouched
            self.assertEqual(of(ev, "hypothetical_order", role="entry")[-1]["reason"], "benched:stop_loss")

    def test_house_limit_cancels_unfilled_after_one_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk = make_desk(tmp, bot="house", answers=BULLISH_HOUSE, spread=0.04)
            ev = run(desk, blocks=3)
            statuses = [e["status"] for e in of(ev, "hypothetical_order", role="entry")]
            self.assertEqual(statuses, ["resting", "cancelled", "skipped", "resting"])
            self.assertEqual(of(ev, "hypothetical_fill"), [])

    def test_house_limit_fills_at_its_price_and_maker_fee_when_the_ask_reaches_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk = make_desk(tmp, bot="house", answers=BULLISH_HOUSE, spread=0.04, move_bps=-10, at=1005)
            ev = run(desk, blocks=2)
            (order,) = of(ev, "hypothetical_order", status="resting")
            (fill,) = of(ev, "hypothetical_fill")
            self.assertEqual(fill["order_id"], order["order_id"])
            self.assertEqual(fill["ts"], 1005)
            self.assertAlmostEqual(fill["price"], order["limit_price"])
            self.assertAlmostEqual(fill["fee_usd"], fill["price"] * fill["qty"] * 1 / 1e4)
            self.assertIsNotNone(desk.hypothetical.book.position)

    def test_daily_loss_limit_binds_on_hypothetical_equity(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk = make_desk(tmp)
            desk.hypothetical.day, desk.hypothetical.day_start_equity = utc_day(1000), desk.cfg.starting_cash_usd + 1_000
            ev = run(desk, blocks=2)
            self.assertTrue(of(ev, "order")[0]["would_pass_risk"])  # the shadow check itself is unchanged
            vetoed, skipped = of(ev, "hypothetical_order", role="entry")
            self.assertEqual(vetoed["status"], "vetoed")
            self.assertIn("daily_loss_limit", vetoed["reason"])
            self.assertEqual(skipped["reason"], "benched:risk_veto")
            self.assertIsNone(desk.hypothetical.book.position)

    def test_ledger_changes_no_other_shadow_event(self):
        def events(with_ledger: bool) -> list[str]:
            with tempfile.TemporaryDirectory() as tmp:
                desk = make_desk(tmp, move_bps=-100, at=1500)
                if not with_ledger:
                    desk.hypothetical = None
                ids: dict[str, str] = {}
                out = []
                for e in run(desk, blocks=80):
                    if not e["type"].startswith("hypothetical_"):
                        if "order_id" in e:  # random per run
                            e["order_id"] = ids.setdefault(e["order_id"], f"o{len(ids)}")
                        out.append(json.dumps(e, sort_keys=True))
                return out

        with_ledger = events(True)
        self.assertEqual(with_ledger, events(False))
        self.assertTrue(any('"type": "order"' in e for e in with_ledger))

    def test_a_ledger_failure_is_logged_and_the_block_carries_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk = make_desk(tmp)

            def boom(order):
                raise RuntimeError("simulated ledger bug")

            desk.hypothetical.broker.submit = boom
            ev = run(desk, blocks=1)
            (err,) = of(ev, "hypothetical_error")
            self.assertIn("simulated ledger bug", err["detail"])
            self.assertEqual(len(of(ev, "order")), 1)
            self.assertEqual(len(of(ev, "equity")), 1)

    def test_paper_mode_has_no_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk = make_desk(tmp, mode=Mode.PAPER)
            ev = run(desk, blocks=5)
            self.assertIsNone(desk.hypothetical)
            self.assertTrue(of(ev, "fill"))
            self.assertFalse([e for e in ev if e["type"].startswith("hypothetical_")])


def closed(net: float, gross: float, side: str = "buy", symbol: str = "X", reason: str = "holding_period", did: str = "d1") -> dict:
    return {"type": "hypothetical_trade_closed", "ts": 2.0, "decision_id": did, "exit_reason": reason, "symbol": symbol, "side": side,
            "notional_usd": 250.0, "gross_pnl_usd": gross, "costs_usd": gross - net, "net_pnl_usd": net, "net_bps": net / 250 * 1e4}


class DailyReportTest(unittest.TestCase):
    shadow_equity = {"type": "equity", "ts": 1.0, "mode": "shadow", "equity_usd": 10_000.0, "day_pnl_usd": 0.0}

    def test_shadow_summary_labels_hypothetical_pnl_and_drops_the_zero_pnl_line(self):
        ev = [
            self.shadow_equity,
            {"type": "hypothetical_order", "ts": 1.0, "role": "entry", "status": "filled", "decision_id": "d1"},
            {"type": "hypothetical_fill", "ts": 1.0, "role": "entry", "decision_id": "d1"},
            {"type": "hypothetical_order", "ts": 1.0, "role": "entry", "status": "skipped", "reason": "position_open", "decision_id": "d2"},
            {"type": "hypothetical_order", "ts": 1.0, "role": "entry", "status": "resting", "decision_id": "d3"},
            {"type": "hypothetical_order", "ts": 1.0, "role": "entry", "status": "cancelled", "decision_id": "d3"},
            {"type": "hypothetical_order", "ts": 1.0, "role": "entry", "status": "filled", "decision_id": "d4"},
            {"type": "hypothetical_fill", "ts": 1.0, "role": "entry", "decision_id": "d4"},
            closed(net=1.0, gross=1.3),
            closed(net=-0.5, gross=-0.2, side="sell", reason="stop_loss", did="d0"),  # entered the day before
        ]
        md = summarize(ev)
        self.assertNotIn("mark-to-market): $0.00", md)
        self.assertIn("PnL: none, shadow mode executes nothing", md)
        self.assertIn("## Hypothetical trades (simulated, not real)", md)
        self.assertIn("Would-be entries offered: 4. Taken: 3. Skipped: 1 (position_open: 1). Vetoed on hypothetical equity: 0.", md)
        self.assertIn("Entries filled: 2. Resting limits cancelled unfilled: 1.", md)
        self.assertIn("Round trips closed: 2 (holding_period: 1, stop_loss: 1). Wins / losses after costs: 1 / 1.", md)
        self.assertIn("Hypothetical PnL after fees and gas: $0.50 (gross $1.10 at simulated fill prices, fees and gas $0.60)", md)
        self.assertIn("Mean per trade: gross +22.00 bps, fees and gas 12.00 bps, net +10.00 bps", md)
        self.assertIn("Filled entries with no close in this log: 1", md)  # d4
        self.assertIn("| X | long | 1 | 1 | $1.00 | +40.00 |", md)
        self.assertIn("| X | short | 1 | 0 | $-0.50 | -20.00 |", md)

    def test_shadow_day_without_ledger_events_says_so(self):
        md = summarize([self.shadow_equity])
        self.assertIn("None logged: no would-be entry passed risk, or this log predates the hypothetical ledger.", md)
        self.assertNotIn("mark-to-market): $0.00", md)

    def test_no_round_trip_is_not_reported_as_zero_pnl(self):
        md = summarize([self.shadow_equity, {"type": "hypothetical_order", "ts": 1.0, "role": "entry", "status": "resting", "decision_id": "d1"}])
        self.assertIn("Hypothetical PnL: none yet, no round trip closed", md)

    def test_paper_summary_is_labeled_simulated_and_has_no_hypothetical_section(self):
        ev = [{"type": "equity", "ts": 1.0, "mode": "paper", "equity_usd": 10_001.0, "day_pnl_usd": 1.0}]
        md = summarize(ev)
        self.assertIn("Paper (simulated) PnL after fees and gas (mark-to-market): $1.00", md)
        self.assertNotIn("Hypothetical", md)


if __name__ == "__main__":
    unittest.main()
