import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cloud" / "budget_guard"))
from decide import instances_to_stop, parse_budget_message, should_stop  # noqa: E402


def msg(cost, budget=10.0, name="ltppxt-monthly", **extra):
    return {"budgetDisplayName": name, "costAmount": cost, "budgetAmount": budget, "currencyCode": "USD", **extra}


class BudgetGuardTest(unittest.TestCase):
    def test_stops_at_or_past_the_line(self):
        self.assertTrue(should_stop(msg(10.0), 1.0, "ltppxt-monthly")[0])
        self.assertTrue(should_stop(msg(12.5), 1.0, "ltppxt-monthly")[0])
        self.assertFalse(should_stop(msg(9.99), 1.0, "ltppxt-monthly")[0])
        self.assertTrue(should_stop(msg(15.0), 1.5, None)[0])
        self.assertFalse(should_stop(msg(14.0), 1.5, None)[0])

    def test_forecast_alone_never_stops(self):
        m = msg(2.0, forecastThresholdExceeded=1.0)
        self.assertFalse(should_stop(m, 1.0, None)[0])

    def test_other_budgets_are_ignored(self):
        stop, why = should_stop(msg(999, name="someone-else"), 1.0, "ltppxt-monthly")
        self.assertFalse(stop)
        self.assertIn("ignoring", why)

    def test_bad_or_zero_numbers_never_stop(self):
        for m in ({"budgetDisplayName": "x"}, msg("abc"), msg(5, budget=0), msg(None)):
            self.assertFalse(should_stop(m, 1.0, None)[0], m)
        self.assertFalse(should_stop(msg(99), 0.0, None)[0])

    def test_parse_rejects_non_objects(self):
        self.assertEqual(parse_budget_message(json.dumps(msg(1)).encode())["costAmount"], 1)
        with self.assertRaises(ValueError):
            parse_budget_message(b"[1,2]")

    def test_only_running_labelled_vms(self):
        vms = [
            {"name": "desk", "zone": "us-central1-a", "status": "RUNNING", "labels": {"ltppxt": "desk"}},
            {"name": "desk-off", "zone": "us-central1-a", "status": "TERMINATED", "labels": {"ltppxt": "desk"}},
            {"name": "other", "zone": "us-central1-a", "status": "RUNNING", "labels": {"team": "x"}},
            {"name": "nolabels", "zone": "us-east1-b", "status": "RUNNING", "labels": None},
        ]
        self.assertEqual([v["name"] for v in instances_to_stop(vms, "ltppxt", "desk")], ["desk"])


if __name__ == "__main__":
    unittest.main()
