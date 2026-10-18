import unittest
from dataclasses import replace

from desk.config import DeskConfig
from policy.policy import Action, decide, threshold
from questions import BOTS, OTHER_LABEL, load_battery
from questions.battery import choice
from tests.helpers import BULLISH_GAMBLER, choice as c, make_snap, noul, score


class QuestionsTest(unittest.TestCase):
    def test_every_choice_has_other(self):
        for bot in BOTS:
            for q in load_battery(bot, 300).questions:
                if q.kind == "choice":
                    self.assertIn(OTHER_LABEL, q.criteria, f"{bot}.{q.id}")

    def test_cannot_pass_other_by_hand(self):
        with self.assertRaises(ValueError):
            choice("x", "?", {"a": "a", "other": "o"})

    def test_wire_shape(self):
        wire = load_battery("house", 300).to_wire()
        self.assertEqual(wire["toxic_flow"]["type"], "noul")
        self.assertIsInstance(wire["liquidity_calm"]["criteria"], list)
        self.assertIn("5 minutes", wire["toxic_flow"]["instructions"])

    def test_resolvers(self):
        b = load_battery("gambler", 300)
        now, up = make_snap(mid=100.0, spread_bps=2.0), make_snap(mid=100.1)
        self.assertEqual(b.by_id("direction").resolver(now, up), "up")
        self.assertIsNone(b.by_id("exit_now").resolver(now, up))  # flat book: nothing to resolve
        self.assertTrue(b.by_id("exit_now").resolver(make_snap(inventory=-1.0), up))


class PolicyTest(unittest.TestCase):
    cfg = DeskConfig()

    def test_threshold_rises_with_cost(self):
        self.assertLess(threshold(Action.ENTER_LONG, 20, 2), threshold(Action.ENTER_LONG, 20, 10))
        self.assertEqual(threshold(Action.ENTER_LONG, 20, 25), 1.0)
        self.assertEqual(threshold(Action.ENTER_LONG, 100, 0), 0.60)  # floor

    def test_exit_bar_lower_than_entry(self):
        snap = make_snap(inventory=1.0)
        d = decide("gambler", BULLISH_GAMBLER, snap, self.cfg, has_position=True)
        entry = decide("gambler", BULLISH_GAMBLER, make_snap(), self.cfg, has_position=False)
        self.assertLess(d.scores["exit"].threshold, entry.scores["enter_long"].threshold)

    def test_bullish_enters_long(self):
        d = decide("gambler", BULLISH_GAMBLER, make_snap(), self.cfg, has_position=False)
        self.assertEqual(d.action, Action.ENTER_LONG)
        self.assertGreaterEqual(d.gross_edge_bps, d.cost_bps * self.cfg.risk.fee_edge_margin)

    def test_uncertain_holds(self):
        flat = {**BULLISH_GAMBLER, "direction": c({"up": 0.3, "down": 0.3, "flat": 0.3, "other": 0.1})}
        self.assertEqual(decide("gambler", flat, make_snap(), self.cfg, False).action, Action.HOLD)

    def test_no_short_when_disabled(self):
        bearish = {**BULLISH_GAMBLER, "direction": c({"up": 0.01, "down": 0.97, "flat": 0.01, "other": 0.01}),
                   "regime": c({"trending_up": 0.0, "trending_down": 1.0, "ranging": 0.0, "disorderly": 0.0, "other": 0.0})}
        self.assertEqual(decide("gambler", bearish, make_snap(), self.cfg, False).action, Action.ENTER_SHORT)
        cfg = replace(self.cfg, allow_short=False)
        self.assertNotEqual(decide("gambler", bearish, make_snap(), cfg, False).action, Action.ENTER_SHORT)

    def test_house_and_spy_features(self):
        house = {"toxic_flow": noul(0.05), "quote_lean": c({"bid": 0.95, "ask": 0.02, "neither": 0.02, "other": 0.01}),
                 "liquidity_calm": score([0, 0, 0.1, 0.9]), "inventory_adverse": noul(0.2)}
        d = decide("house", house, make_snap(), self.cfg, False)
        self.assertTrue(d.passive)
        spy = {"informed_side": c({"buyers": 0.9, "sellers": 0.05, "none": 0.04, "other": 0.01}),
               "imbalance_persists": noul(0.9), "follow_through": noul(0.9), "exit_now": noul(0.1)}
        d = decide("spy", spy, make_snap(imbalance=-0.5), self.cfg, False)
        self.assertEqual(d.scores["enter_long"].contributions["follow_long"], 0.0)


if __name__ == "__main__":
    unittest.main()
