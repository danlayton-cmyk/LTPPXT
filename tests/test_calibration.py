import unittest

from reports.calibration import collect_pairs, render
from tests.helpers import choice, noul


def answer(did, battery, answers, model="jev-1.13.0"):
    return {"type": "answer", "decision_id": did, "model": model, "battery_version": battery, "answers": answers}


def outcome(did, outcomes):
    return {"type": "outcome", "decision_id": did, "outcomes": outcomes}


EVENTS = [
    answer("d1", "house-v1", {"quote_lean": choice({"bid": 0.4, "ask": 0.4, "neither": 0.15, "other": 0.05}), "toxic_flow": noul(0.6)}),
    outcome("d1", {"quote_lean": "neither", "toxic_flow": True, "inventory_adverse": None}),
    answer("d2", "house-v2", {"quote_lean": choice({"bid": 0.7, "ask": 0.1, "neither": 0.15, "other": 0.05}), "toxic_flow": noul(0.2)}),
    outcome("d2", {"quote_lean": "bid", "toxic_flow": False, "inventory_adverse": None}),
]


class CalibrationReportTest(unittest.TestCase):
    def sections(self, md):
        return {s.split("\n", 1)[0]: s for s in md.split("\n## ")[1:]}

    def test_battery_versions_are_never_pooled(self):
        md, svg = render(collect_pairs(EVENTS))
        sections = self.sections(md)
        self.assertEqual(set(sections), {"jev-1.13.0, house-v1", "jev-1.13.0, house-v2"})
        # v1: 0.16 + 0.16 + 0.85^2 + 0.05^2 + 0.4^2 over 5 pairs; v2: 0.3^2 + 0.1^2 + 0.15^2 + 0.05^2 + 0.2^2 over 5
        self.assertIn("Pairs: 5. Brier score: 0.2410", sections["jev-1.13.0, house-v1"])
        self.assertIn("Pairs: 5. Brier score: 0.0330", sections["jev-1.13.0, house-v2"])
        self.assertNotIn("0.1370", md)  # the pooled score
        self.assertIn("house-v1", svg)
        self.assertIn("house-v2", svg)

    def test_per_label_rates(self):
        sections = self.sections(render(collect_pairs(EVENTS))[0])
        v1 = sections["jev-1.13.0, house-v1"]
        self.assertIn("| house-v1.quote_lean | neither | 1 | 0.150 | 1.000 |", v1)
        self.assertIn("| house-v1.quote_lean | bid | 1 | 0.400 | 0.000 |", v1)
        self.assertIn("| house-v1.toxic_flow | yes | 1 | 0.600 | 1.000 |", v1)
        self.assertNotIn("house-v2", v1)

    def test_unresolved_and_fake_answers_are_skipped(self):
        events = [*EVENTS, answer("d3", "house-v2", {"toxic_flow": noul(0.9)}, model="offline-fake"), outcome("d3", {"toxic_flow": True})]
        pairs = collect_pairs(events)
        self.assertEqual(len(pairs), 10)
        self.assertFalse(any(p.model == "offline-fake" for p in pairs))
        self.assertEqual(len(collect_pairs(events, include_fake=True)), 11)


if __name__ == "__main__":
    unittest.main()
