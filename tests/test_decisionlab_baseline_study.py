import csv
import json
import math
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None

if np is not None:
    from decisionlab.baseline_study import build_rows, minute_grid, render, rolling_rate, study
    from decisionlab.candles import fetch_range, load, sha256

START = 1_786_579_200  # 2026-08-13T00:00:00Z
SYMS = ("AAA-USD", "BBB-USD")


def write_candles(root: Path, days: int, seed: int, drift_from_minute: int | None = None, skip: set[int] = frozenset()) -> None:
    """A seeded random walk per symbol. From `drift_from_minute` on, prices take a different path."""
    n = days * 1440
    files = {}
    for k, sym in enumerate(SYMS):
        rng = np.random.default_rng(seed + k)
        steps = rng.normal(0, 4e-4, n)
        if drift_from_minute is not None:
            steps[drift_from_minute:] = np.random.default_rng(seed + 100 + k).normal(0, 4e-4, n - drift_from_minute)
        close = 100 * (k + 1) * np.exp(np.cumsum(steps))
        path = root / f"{sym}.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "low", "high", "open", "close", "volume"])
            for i in range(n):
                if i not in skip:
                    w.writerow([START + i * 60, close[i], close[i], close[i], close[i], 1.0 + (i % 7)])
        files[sym] = {"rows": n - len(skip), "expected_minutes": n, "sha256": sha256(path)}
    iso = lambda ts: datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    (root / "manifest.json").write_text(json.dumps({"source": "synthetic", "start": iso(START), "end": iso(START + n * 60), "files": files}))


@unittest.skipIf(np is None, "numpy not installed (pip install -e '.[research]')")
class BaselineStudyTest(unittest.TestCase):
    def test_features_never_see_the_future(self):
        """Change every price after a cut: rows decided at or before the cut keep identical features."""
        cut = 3 * 1440
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            write_candles(Path(a), 5, seed=1)
            write_candles(Path(b), 5, seed=1, drift_from_minute=cut)
            for h in (5, 30):
                ra, rb = build_rows(Path(a), h, SYMS), build_rows(Path(b), h, SYMS)
                known = ra["t"] <= START + cut * 60  # decided once candle cut-1 has closed
                self.assertGreater(known.sum(), 100)
                np.testing.assert_array_equal(ra["x"][known], rb["x"][known])
                later = ra["t"] > START + (cut + 300) * 60
                self.assertFalse(np.array_equal(ra["x"][later], rb["x"][later]))

    def test_labels_do_not_overlap_within_a_symbol(self):
        with tempfile.TemporaryDirectory() as a:
            write_candles(Path(a), 3, seed=2)
            for h in (5, 30):
                r = build_rows(Path(a), h, SYMS)
                for s in range(len(SYMS)):
                    t = r["t"][r["sym"] == s]
                    self.assertTrue((np.diff(t) >= h * 60).all())

    def test_missing_minutes_are_carried_forward_and_never_decided_on(self):
        rows = [[START, 1, 1, 1, 10.0, 2.0], [START + 120, 1, 1, 1, 12.0, 3.0]]
        close, vol, have = minute_grid(rows, START, START + 180)
        self.assertEqual(list(close), [10.0, 10.0, 12.0])
        self.assertEqual(list(vol), [2.0, 0.0, 3.0])
        self.assertEqual(list(have), [True, False, True])
        with tempfile.TemporaryDirectory() as a:
            write_candles(Path(a), 2, seed=3, skip={600})
            r = build_rows(Path(a), 5, SYMS)
            self.assertNotIn(START + 601 * 60, set(r["t"]))  # minute 600 has no candle, so no decision is made on it

    def test_rolling_rate_is_causal(self):
        y = np.array([1.0] * 30 + [0.0] * 30)
        p = rolling_rate(y, np.zeros(60), prior=0.5, n=20)
        self.assertEqual(p[0], 0.5)  # nothing resolved yet
        self.assertAlmostEqual(p[30], 0.99)  # the first 0 is forecast from the 1s before it
        self.assertAlmostEqual(p[59], 0.01)

    def test_random_walk_shows_no_directional_skill(self):
        with tempfile.TemporaryDirectory() as a:
            write_candles(Path(a), 30, seed=4)
            res = study(Path(a), SYMS, horizons=(5,), n_boot=400)
            up = [c for c in res["cells"] if c["target"] == "up" and c["fold"] == "test" and c["model"] != "base_rate"]
            self.assertEqual(len(up), 4)
            for c in up:
                self.assertGreater(c["ci_hi"], 0, c)  # interval includes "no better than the base rate"
                self.assertGreater(c["p_holm"], 0.05, c)
            self.assertEqual(res["tests_in_family"], 8)
            md = render(res, json.loads((Path(a) / "manifest.json").read_text()))
            self.assertIn("impossible", md)  # a 5-minute random-walk move cannot pay 140 bps
            self.assertEqual(res["cells"], study(Path(a), SYMS, horizons=(5,), n_boot=400)["cells"])  # deterministic

    def test_fetch_range_pages_dedupes_and_sorts(self):
        calls = []

        class Resp:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.body

        def opener(req, timeout):
            calls.append(req.full_url)
            lo = int(datetime.fromisoformat(req.full_url.split("start=")[1].split("&")[0].replace("Z", "+00:00")).timestamp())
            return Resp(json.dumps([[lo + 60, 1, 2, 1, 1.5, 9], [lo, 1, 2, 1, 1.0, 9], [lo, 1, 2, 1, 1.0, 9], [lo - 60, 0, 0, 0, 0, 0]]).encode())

        rows = fetch_range("AAA-USD", START, START + 2 * 300 * 60, opener=opener, sleep=lambda s: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual([r[0] for r in rows], [START, START + 60, START + 18000, START + 18060])  # out-of-range row dropped
        self.assertTrue(math.isclose(rows[1][4], 1.5))


if __name__ == "__main__":
    unittest.main()
