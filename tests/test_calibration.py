import unittest

from reports.calibration import Pair, auc, collect_pairs, fifths, independent_windows, render, source_of
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


def pair(p, happened, did="d", ts=None, outcome_ts=None, label="yes"):
    return Pair("m", "b", "b.q", label, p, happened, decision_id=did, ts=ts, outcome_ts=outcome_ts)


class SkillTest(unittest.TestCase):
    def test_source_from_field_or_decision_id(self):
        self.assertEqual(source_of({"decision_id": "house-BTC-USD-exit-1"}), "exit")
        self.assertEqual(source_of({"decision_id": "house-BTC-USD-sample-1"}), "schedule")
        self.assertEqual(source_of({"decision_id": "house-BTC-USD-1"}), "funnel")
        self.assertEqual(source_of({"decision_id": "house-BTC-USD-1", "source": "schedule"}), "schedule")

    def test_sources_get_separate_sections(self):
        events = [*EVENTS, answer("house-BTC-USD-exit-5", "house-v2", {"toxic_flow": noul(0.3)}), outcome("house-BTC-USD-exit-5", {"toxic_flow": True})]
        sections = CalibrationReportTest.sections(None, render(collect_pairs(events))[0])
        self.assertIn("jev-1.13.0, house-v2, exit", sections)
        self.assertIn("Pairs: 5.", sections["jev-1.13.0, house-v2"])

    def test_auc(self):
        self.assertEqual(auc([pair(0.9, True), pair(0.1, False)])[0], 1.0)
        self.assertEqual(auc([pair(0.1, True), pair(0.9, False)])[0], 0.0)
        self.assertEqual(auc([pair(0.5, True), pair(0.5, False)])[0], 0.5)  # ties count half
        self.assertIsNone(auc([pair(0.5, True)]))
        a, lo, hi = auc([pair(0.1 * k, k % 3 == 0) for k in range(10)])
        self.assertLessEqual(lo, a)
        self.assertLessEqual(a, hi)

    def test_fifths_rank_by_prediction(self):
        rows = fifths([pair(k / 10, k >= 5) for k in range(10)])
        self.assertEqual([n for n, _, _ in rows], [2] * 5)
        self.assertEqual([ob for _, _, ob in rows], [0, 0, 0.5, 1, 1])

    def test_independent_windows(self):
        ps = [pair(0.5, True, "a", 0, 300), pair(0.5, True, "a", 0, 300), pair(0.5, True, "b", 100, 400), pair(0.5, True, "c", 300, 600)]
        self.assertEqual(independent_windows(ps), 2)

    def test_skill_section_scores_other_separately(self):
        md = render(collect_pairs(EVENTS))[0]
        self.assertIn("### Skill", md)
        self.assertIn('| Jev, "other" label only | 1 |', md)
        self.assertIn("| hindsight base rate per label |", md)


if __name__ == "__main__":
    unittest.main()
