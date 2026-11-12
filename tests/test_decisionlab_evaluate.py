import unittest

try:
    import numpy as np
except ImportError:  # the desk itself is stdlib-only; research extras are optional
    np = None

if np is not None:
    from decisionlab.evaluate import (Logistic, auc, block_bootstrap_mean, brier, brier_difference, holm, log_loss,
                                      purged_chronological_split)


@unittest.skipIf(np is None, "numpy not installed (pip install -e '.[research]')")
class EvaluateTest(unittest.TestCase):
    def test_split_is_chronological_and_purged(self):
        t = np.repeat(np.arange(100.0), 3)  # three symbols per timestamp
        train, val, test = purged_chronological_split(t, (0.6, 0.2, 0.2), purge=5)
        self.assertLess(t[train].max(), t[val].min())
        self.assertLess(t[val].max(), t[test].min())
        self.assertGreaterEqual(t[val].min() - t[train].max(), 5)
        self.assertGreaterEqual(t[test].min() - t[val].max(), 5)
        for fold in (train, val, test):  # rows sharing a timestamp stay together
            self.assertEqual(len(fold) % 3, 0)
        self.assertFalse(set(train) & set(val) or set(val) & set(test))

    def test_split_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            purged_chronological_split([1, 2, 3], (0.5, 0.6))
        with self.assertRaises(ValueError):
            purged_chronological_split([1, 2, 3], (0.5, 0.5), purge=-1)
        with self.assertRaises(ValueError):
            purged_chronological_split([1, 1, 1], (0.5, 0.5))

    def test_scores(self):
        self.assertAlmostEqual(brier([0.5, 0.5], [1, 0]), 0.25)
        self.assertAlmostEqual(brier([1, 0], [1, 0]), 0.0)
        self.assertAlmostEqual(log_loss([0.5, 0.5], [1, 0]), 0.6931471805599453)
        self.assertAlmostEqual(auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]), 0.75)
        self.assertAlmostEqual(auc([0.5, 0.5, 0.5], [0, 1, 1]), 0.5)  # ties score as chance
        self.assertIsNone(auc([0.2, 0.3], [1, 1]))

    def test_block_bootstrap_is_wider_than_iid_when_blocks_are_correlated(self):
        rng = np.random.default_rng(1)
        block_effect = rng.normal(0, 1, 40)
        values = np.repeat(block_effect, 50) + rng.normal(0, 0.1, 2000)
        by_block = block_bootstrap_mean(values, np.repeat(np.arange(40), 50), seed=3)
        iid = block_bootstrap_mean(values, np.arange(2000), seed=3)
        self.assertGreater(by_block["hi"] - by_block["lo"], 4 * (iid["hi"] - iid["lo"]))
        self.assertEqual(by_block["n_blocks"], 40)
        self.assertEqual(by_block, block_bootstrap_mean(values, np.repeat(np.arange(40), 50), seed=3))  # seeded

    def test_null_model_shows_no_skill_and_real_signal_does(self):
        rng = np.random.default_rng(7)
        n = 6000
        x = rng.normal(size=(n, 3))
        y_null = rng.random(n) < 0.35
        y_real = rng.random(n) < 1 / (1 + np.exp(-(1.2 * x[:, 0] - 0.6)))
        tr, te = np.arange(4000), np.arange(4000, n)
        days = np.arange(n) // 100
        for y, expect_skill in ((y_null, False), (y_real, True)):
            p = Logistic().fit(x[tr], y[tr]).predict(x[te])
            base = np.full(len(te), y[tr].mean())
            d = brier_difference(p, base, y[te], days[te], seed=0)
            self.assertEqual(d["hi"] < 0, expect_skill, d)

    def test_holm(self):
        adj = holm({"a": 0.01, "b": 0.04, "c": 0.03})
        self.assertAlmostEqual(adj["a"], 0.03)
        self.assertAlmostEqual(adj["c"], 0.06)
        self.assertAlmostEqual(adj["b"], 0.06)  # monotone: never below an earlier adjusted value


if __name__ == "__main__":
    unittest.main()
