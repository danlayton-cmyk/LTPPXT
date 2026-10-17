import ast
import pathlib
import unittest

from book.book import Bench, Book, OpenPosition
from broker import make_broker
from broker.base import Order
from broker.paper import PaperBroker
from desk.config import DEFAULT_BENCH_SECONDS, PINNED_JEV_MODEL, DeskConfig, FeeConfig, Mode, RiskConfig, check_pinned_model
from risk.vetoes import GuardedBroker, RiskContext, VetoedOrder, check_order
from state.snapshot import Tick

RISK = RiskConfig()
CTX = RiskContext(mid=100.0, prev_mid=100.0, data_age_s=1.0, position_qty=0.0, gross_exposure_usd=0.0, equity_usd=10_000, day_pnl_usd=0.0)


def order(**over):
    base = dict(id="o1", symbol="BTC-USD", side="buy", qty=2.0, type="market", limit_price=None, reduce_only=False, created_ts=0.0)
    base.update(over)
    return Order(**base)


def checks(o, ctx=CTX, model=PINNED_JEV_MODEL, edge=30.0, cost=10.0, cfg=RISK):
    return {v.check for v in check_order(o, ctx, cfg, PINNED_JEV_MODEL, model, edge, cost)}


class RiskTest(unittest.TestCase):
    def test_clean_order_passes(self):
        self.assertEqual(checks(order()), set())

    def test_each_veto_fires(self):
        self.assertIn("max_position", checks(order(qty=11)))
        self.assertIn("daily_loss_limit", checks(order(), ctx=CTX.__class__(**{**CTX.__dict__, "day_pnl_usd": -50})))
        self.assertIn("leverage_cap", checks(order(), ctx=CTX.__class__(**{**CTX.__dict__, "gross_exposure_usd": 9_900})))
        self.assertIn("range_guard", checks(order(type="limit", limit_price=101.0)))
        self.assertIn("range_guard", checks(order(), ctx=CTX.__class__(**{**CTX.__dict__, "prev_mid": 90.0})))
        self.assertIn("range_guard", checks(order(), ctx=CTX.__class__(**{**CTX.__dict__, "data_age_s": 30})))
        self.assertIn("fees_plus_gas", checks(order(), edge=14.9, cost=10.0))
        self.assertIn("model_version", checks(order(), model="jev-1.14.0"))
        self.assertIn("model_version", checks(order(), model="jev-latest"))
        self.assertIn("model_version", checks(order(), model=None))

    def test_reduce_only_skips_exposure_checks_but_not_guards(self):
        exit_ = order(side="sell", reduce_only=True)
        ctx = CTX.__class__(**{**CTX.__dict__, "day_pnl_usd": -500, "position_qty": 2.0})
        self.assertEqual(checks(exit_, ctx=ctx, model=None, edge=None), set())
        self.assertIn("model_version", checks(exit_, ctx=ctx, model="jev-1.14.0", edge=None))
        self.assertIn("range_guard", checks(exit_, ctx=CTX.__class__(**{**ctx.__dict__, "data_age_s": 60}), model=None, edge=None))

    def test_guarded_broker_blocks(self):
        b = PaperBroker(10_000, FeeConfig())
        b.on_tick(Tick(0, "BTC-USD", 99.99, 100.01, 10, 10))
        g = GuardedBroker(b, RISK, PINNED_JEV_MODEL)
        with self.assertRaises(VetoedOrder):
            g.submit(order(), CTX, "jev-1.14.0", 30.0, 10.0)
        self.assertEqual(b.fills, [])
        self.assertIsNotNone(g.submit(order(), CTX, PINNED_JEV_MODEL, 30.0, 10.0))

    def test_model_pin_refuses_aliases(self):
        for bad in ("jev-latest", "jev-preview", "jev-1.13", "gpt-x"):
            with self.assertRaises(ValueError):
                check_pinned_model(bad)
        check_pinned_model("jev-1.13.0")


class PaperOnlyTest(unittest.TestCase):
    def test_no_live_mode(self):
        self.assertEqual({m.value for m in Mode}, {"shadow", "paper"})
        self.assertFalse(make_broker(DeskConfig(mode=Mode.PAPER)).is_live)

    def test_no_network_in_trading_code(self):
        """Trading packages import no HTTP or socket modules. Only judge/client.py talks to the network, and only to the judge."""
        root = pathlib.Path(__file__).resolve().parents[1]
        banned = {"socket", "http", "urllib", "requests", "httpx", "httpx2", "aiohttp", "websockets", "typesafe_sdk"}
        for pkg in ("state", "questions", "policy", "risk", "broker", "filter", "book", "logs"):
            for path in (root / pkg).glob("*.py"):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                    for n in names:
                        self.assertNotIn(n.split(".")[0], banned, f"{path} imports {n}")


class PaperBrokerTest(unittest.TestCase):
    def test_round_trip_pnl_after_fees(self):
        fees = FeeConfig(taker_fee_bps=10, maker_fee_bps=0, gas_usd_per_fill=0.5, slippage_bps=0)
        b = PaperBroker(1_000, fees)
        b.on_tick(Tick(0, "X", 99, 101, 10, 10))
        b.submit(order(symbol="X", qty=1))  # buy at 101
        b.on_tick(Tick(1, "X", 109, 111, 10, 10))
        b.submit(order(id="o2", symbol="X", side="sell", qty=5, reduce_only=True))  # clipped to 1, sell at 109
        self.assertEqual(b.position("X").qty, 0)
        expected = 8 - (101 + 109) * 10 / 1e4 - 1.0
        self.assertAlmostEqual(b.realized_pnl_usd, expected)
        self.assertAlmostEqual(b.cash_usd(), 1_000 + expected)

    def test_limit_rests_then_fills(self):
        b = PaperBroker(1_000, FeeConfig(maker_fee_bps=0, gas_usd_per_fill=0))
        b.on_tick(Tick(0, "X", 99, 101, 10, 10))
        self.assertIsNone(b.submit(order(symbol="X", qty=1, type="limit", limit_price=99.0)))
        self.assertEqual(len(b.open_orders()), 1)
        fills = b.on_tick(Tick(1, "X", 98, 99, 10, 10))
        self.assertEqual(fills[0].price, 99.0)


class BookBenchTest(unittest.TestCase):
    def test_one_position_at_a_time(self):
        book = Book()
        self.assertTrue(book.can_scan())
        book.open(OpenPosition("X", "buy", 1, 100, 0, "d1"))
        self.assertFalse(book.can_scan())
        with self.assertRaises(RuntimeError):
            book.open(OpenPosition("Y", "buy", 1, 100, 0, "d2"))

    def test_bench_duration_set_by_reason(self):
        bench = Bench(DEFAULT_BENCH_SECONDS)
        self.assertEqual(bench.bench("A", "stale_data", 0).until, 30)
        self.assertEqual(bench.bench("B", "jev_malformed", 0).until, 86_400)
        self.assertEqual(bench.bench("C", "below_threshold:enter_long", 0).until, 120)
        bench.bench("A", "risk_veto", 10)  # longer reason extends
        self.assertEqual(bench.benched("A", 20).check, "risk_veto")
        self.assertIsNone(bench.benched("C", 121))


if __name__ == "__main__":
    unittest.main()
