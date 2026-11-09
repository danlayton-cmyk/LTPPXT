"""Regression tests for the DecisionLab research policy."""

import unittest

from decisionlab.policy import evaluate_decision_policy


class DecisionPolicyTests(unittest.TestCase):
    def test_missing_and_insufficient_samples_hold(self):
        for sample_size in (None, 0, 99):
            with self.subTest(sample_size=sample_size):
                result = evaluate_decision_policy(0.99, sample_size=sample_size)
                self.assertEqual(result, {"action": "HOLD", "confidence": 0.99, "gate": "FAIL_SAMPLE_SIZE"})

    def test_minimum_and_threshold_are_inclusive(self):
        self.assertEqual(evaluate_decision_policy(0.65, sample_size=100)["action"], "EXECUTE")
        self.assertEqual(evaluate_decision_policy(0.64, sample_size=100)["gate"], "FAIL_THRESHOLD")
        self.assertEqual(evaluate_decision_policy(0.99, min_sample_size=101, sample_size=100)["gate"], "FAIL_SAMPLE_SIZE")

    def test_invalid_controls_fail(self):
        for args in (
            {"sample_size": True}, {"sample_size": -1}, {"sample_size": 100.0},
            {"min_sample_size": 0}, {"min_sample_size": True}, {"threshold": 1.1},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                evaluate_decision_policy(0.99, **args)
        with self.assertRaises(ValueError):
            evaluate_decision_policy(float("nan"), sample_size=100)
