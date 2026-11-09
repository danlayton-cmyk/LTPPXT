"""Regression tests for the DecisionLab calibration audit."""

import unittest

import numpy as np
import pandas as pd

from decisionlab.calibration import purged_walk_forward_split, run_calibration_audit


def make_frame(seconds, targets=None):
    timestamps = pd.Timestamp("2026-11-09", tz="UTC") + pd.to_timedelta(seconds, unit="s")
    if targets is None:
        targets = np.arange(len(seconds)) % 2
    return pd.DataFrame({"timestamp": timestamps, "feature": targets, "target": targets})


class SplitTests(unittest.TestCase):
    def test_default_gap_boundary_and_original_positions(self):
        frame = make_frame([3601, 0, 3599, 1, 1, 7201])
        frame.index = [77, 77, 9, 15, 18, -1]
        train, test = purged_walk_forward_split(frame, train_ratio=0.5)
        self.assertEqual(train.tolist(), [1, 3, 4])
        self.assertEqual(test.tolist(), [0, 5])
        self.assertFalse(set(train) & set(test))
        gap = frame.iloc[test].timestamp.min() - frame.iloc[train].timestamp.max()
        self.assertEqual(gap.total_seconds(), 3600)

    def test_zero_gap_still_keeps_equal_timestamps_together(self):
        frame = make_frame([0, 1, 1, 2])
        train, test = purged_walk_forward_split(frame, horizon_sec=0, purge_sec=0, train_ratio=0.5)
        self.assertEqual(train.tolist(), [0, 1, 2])
        self.assertEqual(test.tolist(), [3])

    def test_invalid_split_parameters_and_timestamps(self):
        frame = make_frame([0, 3600])
        for kwargs in ({"train_ratio": 1}, {"train_ratio": -0.1}, {"horizon_sec": -1}, {"purge_sec": np.inf}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                purged_walk_forward_split(frame, **kwargs)
        for timestamp in (pd.NaT, "invalid"):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(ValueError, "valid datetime"):
                purged_walk_forward_split(frame.assign(timestamp=timestamp))

    def test_missing_timestamp_column(self):
        with self.assertRaises(KeyError):
            purged_walk_forward_split(make_frame([0, 3600]), time_col="missing")


class AuditTests(unittest.TestCase):
    def audit(self, frame):
        return run_calibration_audit(frame, ["feature"], "target")

    def test_chronological_model_scores_are_invariant_to_row_order_and_index(self):
        frame = make_frame(np.arange(200) * 120)
        reference = self.audit(frame)
        shuffled = frame.sample(frac=1, random_state=42)
        shuffled.index = np.arange(len(shuffled)) % 11
        self.assertEqual(self.audit(shuffled), reference)
        self.assertEqual(reference["train_samples"], 140)
        self.assertEqual(reference["test_samples"], 31)
        self.assertEqual(reference["brier_constant"], 0.25)
        self.assertLess(reference["brier_causal"], 0.25)
        self.assertEqual(reference["verdict"], "GO")

    def test_empty_inputs_and_empty_holdout(self):
        for seconds in ([], [0], list(range(0, 10000, 10))):
            with self.subTest(size=len(seconds)):
                result = self.audit(make_frame(seconds))
                self.assertEqual(result["reason"], "empty_train_or_test_slice")
                self.assertEqual(result["verdict"], "REVISE/NO-GO")
                self.assertIsNone(result["brier_constant"])
                self.assertIsNone(result["brier_causal"])
                self.assertIsNone(result["delta"])

    def test_single_class_train_scores_prevalence_only(self):
        frame = make_frame([0, 60, 120, 180, 240, 3840, 3900], [0, 0, 0, 0, 0, 0, 1])
        result = self.audit(frame)
        self.assertEqual(result["reason"], "single_class_training_target")
        self.assertAlmostEqual(result["brier_constant"], 0.5)
        self.assertIsNone(result["brier_causal"])
        self.assertEqual(result["verdict"], "REVISE/NO-GO")

    def test_severe_imbalance_and_single_class_test_use_train_prevalence(self):
        labels = np.zeros(200, dtype=int)
        labels[0] = 1
        frame = make_frame(np.arange(200) * 120, labels).assign(feature=0)
        result = self.audit(frame)
        self.assertAlmostEqual(result["brier_constant"], (1 / 140) ** 2)
        self.assertTrue(np.isfinite(result["brier_causal"]))
        self.assertGreaterEqual(result["delta"], 0)
        self.assertEqual(result["verdict"], "REVISE/NO-GO")

    def test_missing_and_nonbinary_targets(self):
        frame = make_frame(np.arange(200) * 120)
        with self.assertRaises(KeyError):
            self.audit(frame.drop(columns="target"))
        for invalid in (np.nan, 2):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "binary labels"):
                self.audit(frame.assign(target=invalid))
