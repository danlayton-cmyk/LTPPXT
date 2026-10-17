import unittest

from desk.config import StateConfig
from state.snapshot import AccountView, Tick, approx_tokens, build_snapshot
from tests.helpers import ticks

CFG = StateConfig()
ACCT = AccountView(inventory=0.0, equity_usd=10_000, peak_equity_usd=10_000)


class SnapshotTest(unittest.TestCase):
    def test_uses_only_ticks_before_decision(self):
        past = ticks(n=1000)
        decision = 1000.0  # last tick is at 999
        base = build_snapshot(past, decision, ACCT, CFG)
        future = [Tick(1000.0, "BTC-USD", 500, 501, 1, 999), Tick(1005.0, "BTC-USD", 1, 2, 999, 1)]
        self.assertEqual(build_snapshot(past + future, decision, ACCT, CFG), base)
        self.assertLess(base.data_ts, decision)

    def test_deterministic(self):
        t = ticks(step=0.01)
        self.assertEqual(build_snapshot(t, 1100, ACCT, CFG), build_snapshot(list(reversed(t)), 1100, ACCT, CFG))

    def test_fields(self):
        snap = build_snapshot(ticks(n=1000, step=0.01), 1000.0, ACCT, CFG)
        self.assertAlmostEqual(snap.mid, 100 + 0.01 * 999)
        self.assertAlmostEqual(snap.spread_bps, 0.02 / snap.mid * 1e4)
        self.assertEqual(snap.imbalance, 0.0)
        self.assertGreater(snap.returns_bps["1m"], 0)
        self.assertGreater(snap.returns_bps["15m"], snap.returns_bps["5m"])
        self.assertGreater(snap.rvol_bps, 0)

    def test_short_history_gives_none_returns(self):
        snap = build_snapshot(ticks(n=100), 100.0, ACCT, CFG)
        self.assertIsNone(snap.returns_bps["15m"])
        self.assertIsNotNone(snap.returns_bps["1m"])

    def test_drawdown_and_inventory(self):
        snap = build_snapshot(ticks(n=10), 10.0, AccountView(-2.5, 9_000, 10_000), CFG)
        self.assertAlmostEqual(snap.drawdown_pct, 10.0)
        self.assertEqual(snap.to_state()["position"], "short")

    def test_under_token_budget(self):
        snap = build_snapshot(ticks(n=1000, price=123456.789, step=-0.123), 1000.0, AccountView(-0.0123456, 98765.43, 123456.7), CFG)
        self.assertLess(approx_tokens(snap.to_json()), 400)

    def test_no_data(self):
        self.assertIsNone(build_snapshot(ticks(start=50), 10.0, ACCT, CFG))


if __name__ == "__main__":
    unittest.main()
