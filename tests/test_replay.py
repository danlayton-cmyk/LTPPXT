import random
import tempfile
import unittest
from pathlib import Path

from judge.client import JudgeResult
from reports.replay import Logistic, load_rows, replay, render, score_rows, lean_gross_bps, thin, add_baselines, v3_candidate
from tests.helpers import choice, noul


def events(n=150, seed=1):
    rng = random.Random(seed)
    out = []
    for k in range(n):
        did, ts = f"house-BTC-USD-{k}", 1000.0 + k * 400  # windows of 300 s never overlap
        imb = rng.uniform(-1, 1)
        move = 8 * imb + rng.gauss(0, 4)  # imbalance predicts direction
        state = {"spread_bps": 1.0, "book_imbalance": imb, "realized_vol_bps": 20.0, "return_bps": {"1m": 0.0, "5m": 0.0, "15m": 0.0}}
        lean = "neither" if abs(move) > 10 or move == 0 else ("bid" if move > 0 else "ask")
        out += [
            {"type": "question", "ts": ts, "decision_id": did, "battery_version": "house-v2", "state": state, "questions": {"q": {}}},
            {"type": "answer", "ts": ts, "decision_id": did, "battery_version": "house-v2", "model": "jev-1.13.0",
             "answers": {"toxic_flow": noul(0.4), "quote_lean": choice({"bid": 0.4, "ask": 0.3, "neither": 0.25, "other": 0.05})}},
            {"type": "outcome", "ts": ts + 300, "decision_id": did, "battery_version": "house-v2", "mid_then": 100.0, "mid_now": 100.0 * (1 + move / 1e4),
             "outcomes": {"toxic_flow": abs(move) > 10, "quote_lean": lean}},
        ]
    return out


class ReplayTest(unittest.TestCase):
    def test_thin_drops_overlapping_windows(self):
        ev = events(10)
        for e in ev:  # squeeze answers 100 s apart so 300 s windows overlap
            if e["type"] in ("question", "answer"):
                e["ts"] = 1000.0 + int(e["decision_id"].rsplit("-", 1)[1]) * 100
            if e["type"] == "outcome":
                e["ts"] = 1300.0 + int(e["decision_id"].rsplit("-", 1)[1]) * 100
        rows = load_rows(ev)
        self.assertEqual(len(rows), 10)
        self.assertEqual(len(thin(rows)), 4)  # 0, 3, 6, 9

    def test_exits_are_left_out(self):
        ev = events(3)
        for e in ev:
            e["decision_id"] = e["decision_id"].replace("USD-", "USD-exit-")
        self.assertEqual(load_rows(ev), [])

    def test_logistic_learns_a_real_signal_and_beats_the_base_rate(self):
        rows = thin(load_rows(events()))
        train, test = rows[:100], rows[100:]
        add_baselines(train, test)
        scores = score_rows(test)
        self.assertGreater(scores["logistic"]["up"]["skill"], 0.1)
        self.assertGreater(scores["logistic"]["up"]["auc"][0], 0.8)
        self.assertAlmostEqual(scores["base rate"]["up"]["skill"], 0.0)
        gross = lean_gross_bps(test)
        self.assertGreater(gross["logistic"][1], 0)

    def test_logistic_on_separable_data(self):
        m = Logistic().fit([[x] for x in range(-5, 6)], [x > 0 for x in range(-5, 6)])
        self.assertGreater(m.predict([4]), 0.8)
        self.assertLess(m.predict([-4]), 0.2)

    def test_replay_caches_and_respects_the_cap(self):
        rows = thin(load_rows(events(20)))
        add_baselines(rows[:10], rows[10:])
        test = rows[10:]
        made = []

        class Client:
            def __init__(self, model):
                self.model, self.calls = model, 0
                made.append(self)

            def ask(self, state, questions, deadline):
                self.calls += 1
                if "big_move" in questions:
                    return JudgeResult(self.model, {"big_move": noul(0.3), "up_move": noul(0.6)}, None, None, 0.1)
                return JudgeResult(self.model, {"toxic_flow": noul(0.2), "quote_lean": choice({"bid": 0.5, "ask": 0.2, "neither": 0.2, "other": 0.1})}, None, None, 0.1)

        plans = [("jev-1.14.0 replay", "jev-1.14.0", "house-v2", None),
                 ("jev-1.13.0 on house-v3-candidate", "jev-1.13.0", "house-v3-candidate", v3_candidate().to_wire())]
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "replay-answers.jsonl"
            dry = replay(test, plans, cache, call=False, max_calls=100, make_client=Client)
            self.assertEqual(dry["calls"], 0)
            self.assertEqual(made, [])
            first = replay(test, plans, cache, call=True, max_calls=15, make_client=Client)
            self.assertEqual(first["calls"], 15)
            second = replay(test, plans, cache, call=True, max_calls=100, make_client=Client)
            self.assertEqual(second["calls"], 2 * len(test) - 15)  # only the uncached ones
            self.assertEqual(len(cache.read_text().splitlines()), 2 * len(test))
            self.assertAlmostEqual(test[0].forecasts["jev-1.13.0 on house-v3-candidate"]["up"], 0.6)
            md = render(20, rows, rows[:10], test, score_rows(test), lean_gross_bps(test), second, True)
            self.assertIn("| jev-1.14.0 replay | bid |", md)

    def test_wrong_model_is_not_cached(self):
        rows = thin(load_rows(events(20)))
        add_baselines(rows[:10], rows[10:])

        class Client:
            def __init__(self, model):
                pass

            def ask(self, state, questions, deadline):
                return JudgeResult("jev-1.13.0", {"toxic_flow": noul(0.2)}, None, None, 0.1)

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c.jsonl"
            stats = replay(rows[10:], [("jev-1.14.0 replay", "jev-1.14.0", "house-v2", None)], cache, True, 100, Client, log=lambda *_: None)
            self.assertFalse(cache.exists())
            self.assertEqual(stats["missing:jev-1.14.0 replay"], 10)


if __name__ == "__main__":
    unittest.main()
