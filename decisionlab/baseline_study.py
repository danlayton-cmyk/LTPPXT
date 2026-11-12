"""Pre-specified baseline study on public 1-minute candles: is anything predictable, and
would it pay for itself?

    python -m decisionlab.candles --out data/candles --days 90 --end 2026-11-11T00:00:00Z
    python -m decisionlab.baseline_study --data data/candles --out reports/out

Everything below was fixed before the holdout was scored. There is no tuning loop: one
regularization strength, one feature set per model, so the experiment count is exactly
the number of cells in the results table, and Holm's correction runs over all of them.

Rows       One decision per symbol every `h` minutes on a shared UTC grid, so a symbol's
           label windows never overlap. Symbols at the same time are correlated; the
           bootstrap resamples whole UTC days, all symbols together, to respect that.
Horizons   5, 30 and 240 minutes.
Targets    big_move: |move| > band(h), the desk's toxic_flow definition (10 bps at 5 minutes,
           scaled by sqrt(time)). up: move > 0.
Features   Known at decision time only: returns over 1, 5, 15, 60 minutes; realized vol over
           15 and 60 minutes; 15-minute volume against its 4-hour average; hour of day;
           symbol. Candle closes stand in for the mid. No order book.
Models     base_rate (training prevalence), rolling_rate (prevalence of the last 288 resolved
           rows per symbol, causal), vol_logit (the two vol features), momentum_logit (the
           four returns), full_logit (everything).
Split      Oldest 60% train, next 20% validation, newest 20% test, by time, with the horizon
           plus 60 minutes of feature lookback purged at each boundary. Nothing is selected
           on validation, so both later folds are out of sample; test is the headline.
Economics  For `up`: take the model's side on every test row, gross bps per trade, then net
           of round-trip cost scenarios. Also a ceiling: what perfect foresight of direction
           would gross, which is the mean absolute move.

Limits: closes are not a tradable mid, there is no spread, depth, queue or fill model, and
90 days is one market regime sample. A null here does not prove no edge exists anywhere.
A positive here would not prove one is tradable.
"""

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from decisionlab.candles import SYMBOLS, load
from decisionlab.evaluate import Logistic, auc, block_bootstrap_mean, brier, brier_difference, holm, log_loss, purged_chronological_split

HORIZONS_MIN = (5, 30, 240)
CEILING_HORIZONS_MIN = (5, 30, 240, 1440)
BAND_BPS_AT_5_MIN = 10.0
LOOKBACK_MIN = 240
ROLLING_N = 288
SEED = 20261111
# Round-trip cost scenarios in bps of notional. See the audit for where each comes from.
COSTS_BPS = {
    "repo default, maker in / maker out (what policy.py prices house at)": 2.0,
    "repo default, maker in / taker out + 1 bp slippage": 7.0,
    "repo default, taker both ways + 1 bp slippage each": 12.0,
    "Coinbase US entry tier per PR #8, maker in / taker out (0.50% + 0.90%)": 140.0,
    "Coinbase US entry tier per PR #8, taker both ways (0.90% x 2)": 180.0,
}


def band_bps(h_min: int) -> float:
    return round(BAND_BPS_AT_5_MIN * math.sqrt(h_min / 5))


def minute_grid(rows: list[list[float]], start: int, end: int):
    """Closes and volumes on a regular 1-minute grid. A minute with no candle (no trades)
    carries the previous close forward with zero volume. `have` marks real candles."""
    n = (end - start) // 60
    close, vol, have = np.full(n, np.nan), np.zeros(n), np.zeros(n, bool)
    for ts, _low, _high, _open, c, v in rows:
        i = (int(ts) - start) // 60
        if 0 <= i < n:
            close[i], vol[i], have[i] = c, v, True
    last = np.nan
    for i in range(n):
        if have[i]:
            last = close[i]
        else:
            close[i] = last
    return close, vol, have


def build_rows(data_dir: Path, h: int, symbols=SYMBOLS) -> dict:
    """One row per symbol per decision time. Index i means "the candle starting at minute i
    has closed": features use closes up to i, the label uses the close at i + h."""
    manifest = json.loads((data_dir / "manifest.json").read_text())
    start = int(datetime.fromisoformat(manifest["start"].replace("Z", "+00:00")).timestamp())
    end = int(datetime.fromisoformat(manifest["end"].replace("Z", "+00:00")).timestamp())
    cols = {k: [] for k in ("t", "sym", "move", "x")}
    for s_idx, sym in enumerate(symbols):
        close, vol, have = minute_grid(load(data_dir / f"{sym}.csv"), start, end)
        lc = np.log(close)
        r1 = np.diff(lc, prepend=np.nan) * 1e4
        sq = np.nan_to_num(r1) ** 2
        csq, cvol = np.cumsum(sq), np.cumsum(vol)
        for i in range(LOOKBACK_MIN, len(close) - h, h):
            if not (have[i] and np.isfinite(lc[i - LOOKBACK_MIN]) and np.isfinite(lc[i + h])):
                continue
            ret = lambda w: (lc[i] - lc[i - w]) * 1e4
            rv = lambda w: math.sqrt(csq[i] - csq[i - w])
            v15, v240 = cvol[i] - cvol[i - 15], cvol[i] - cvol[i - 240]
            hour = ((start + (i + 1) * 60) % 86400) / 86400 * 2 * math.pi
            cols["t"].append(start + (i + 1) * 60)
            cols["sym"].append(s_idx)
            cols["move"].append((lc[i + h] - lc[i]) * 1e4)
            cols["x"].append([ret(1), ret(5), ret(15), ret(60), math.log1p(rv(15)), math.log1p(rv(60)),
                              math.log((v15 / 15 + 1e-12) / (v240 / 240 + 1e-12)) if v240 > 0 else 0.0,
                              math.sin(hour), math.cos(hour), *(1.0 if s_idx == k else 0.0 for k in range(1, len(symbols)))])
    out = {k: np.asarray(v, float) for k, v in cols.items()}
    order = np.lexsort((out["sym"], out["t"]))
    return {k: v[order] for k, v in out.items()}


FEATURE_SETS = {"vol_logit": [4, 5], "momentum_logit": [0, 1, 2, 3], "full_logit": None}


def rolling_rate(y: np.ndarray, sym: np.ndarray, prior: float, n: int = ROLLING_N) -> np.ndarray:
    """Per symbol, the mean of the previous n outcomes. Rows are h apart and the label takes h
    to resolve, so row k's outcome is known when row k+1 is decided: causal."""
    p = np.full(len(y), prior)
    for s in np.unique(sym):
        idx = np.flatnonzero(sym == s)
        c = np.concatenate([[0.0], np.cumsum(y[idx])])
        for k, i in enumerate(idx):
            lo = max(0, k - n)
            if k - lo >= 20:
                p[i] = (c[k] - c[lo]) / (k - lo)
    return np.clip(p, 0.01, 0.99)


def study(data_dir: Path, symbols=SYMBOLS, horizons=HORIZONS_MIN, n_boot: int = 2000) -> dict:
    results = {"cells": [], "economics": [], "folds": {}, "seed": SEED}
    for h in horizons:
        d = build_rows(data_dir, h, symbols)
        t, x, move, sym = d["t"], d["x"], d["move"], d["sym"]
        day = (t // 86400).astype(int)
        train, val, test = purged_chronological_split(t, (0.6, 0.2, 0.2), purge=(h + 60) * 60)
        iso = lambda ts: datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d %H:%M")
        results["folds"][h] = {name: {"rows": int(len(f)), "from": iso(t[f].min()), "to": iso(t[f].max()), "days": int(len(np.unique(day[f])))}
                               for name, f in (("train", train), ("validation", val), ("test", test))}
        for target, y in (("big_move", np.abs(move) > band_bps(h)), ("up", move > 0)):
            y = y.astype(float)
            preds = {"base_rate": np.full(len(y), y[train].mean()), "rolling_rate": rolling_rate(y, sym, y[train].mean())}
            for name, cols in FEATURE_SETS.items():
                xs = x if cols is None else x[:, cols]
                preds[name] = Logistic(l2=1.0).fit(xs[train], y[train]).predict(xs)
            for fold_name, f in (("validation", val), ("test", test)):
                for name, p in preds.items():
                    cell = {"horizon_min": h, "target": target, "model": name, "fold": fold_name, "n": int(len(f)),
                            "rate": float(y[f].mean()), "brier": brier(p[f], y[f]), "log_loss": log_loss(p[f], y[f]), "auc": auc(p[f], y[f])}
                    if name != "base_rate":
                        diff = brier_difference(p[f], preds["base_rate"][f], y[f], day[f], n_boot=n_boot, seed=SEED)
                        cell.update(brier_minus_base=diff["mean"], ci_lo=diff["lo"], ci_hi=diff["hi"], p_no_skill=diff["p_ge_zero"],
                                    skill=1 - cell["brier"] / brier(preds["base_rate"][f], y[f]))
                    results["cells"].append(cell)
            if target == "up":
                for name in ("momentum_logit", "full_logit"):
                    side = np.where(preds[name][test] > 0.5, 1.0, -1.0)
                    g = block_bootstrap_mean(side * move[test], day[test], n_boot=n_boot, seed=SEED)
                    results["economics"].append({"horizon_min": h, "model": name, "trades": g["n"], "gross_bps": g["mean"], "ci_lo": g["lo"], "ci_hi": g["hi"],
                                                 "hit_rate": float((side * move[test] > 0).mean())})
    family = {f"{c['horizon_min']}|{c['target']}|{c['model']}": c["p_no_skill"] for c in results["cells"] if c["fold"] == "test" and "p_no_skill" in c}
    adjusted = holm(family)
    for c in results["cells"]:
        key = f"{c['horizon_min']}|{c['target']}|{c['model']}"
        if c["fold"] == "test" and key in adjusted:
            c["p_holm"] = adjusted[key]
    results["tests_in_family"] = len(family)
    results["ceiling"] = ceiling(data_dir, symbols)
    return results


def ceiling(data_dir: Path, symbols=SYMBOLS) -> list[dict]:
    """Per symbol and horizon: the size of moves, and how often a move even exceeds each
    cost. The mean absolute move is what a trader with perfect foresight of direction would
    gross per trade; no forecaster can beat it."""
    out = []
    for h in CEILING_HORIZONS_MIN:
        d = build_rows(data_dir, h, symbols)
        for s_idx, sym in enumerate(symbols):
            a = np.abs(d["move"][d["sym"] == s_idx])
            row = {"symbol": sym, "horizon_min": h, "n": int(len(a)), "mean_abs_bps": float(a.mean()), "median_abs_bps": float(np.median(a)), "p95_abs_bps": float(np.quantile(a, 0.95))}
            for label, c in COSTS_BPS.items():
                row[f"share_abs_gt_{c:g}"] = float((a > c).mean())
                m = float(a.mean())
                row[f"breakeven_hit_rate_{c:g}"] = (m + c) / (2 * m) if m > 0 else None
            out.append(row)
    return out


def render(res: dict, manifest: dict) -> str:
    f = lambda v, fmt=".4f": "-" if v is None else format(v, fmt)
    L = ["# Baseline study on public candles", "",
         f"Data: Coinbase Exchange public 1-minute candles, {manifest['start']} to {manifest['end']}. "
         + "; ".join(f"{s}: {m['rows']} of {m['expected_minutes']} minutes, sha256 {m['sha256'][:12]}" for s, m in manifest["files"].items()) + ".",
         f"Bootstrap: whole UTC days, seed {res['seed']}. Holm family: {res['tests_in_family']} test-fold comparisons.", "",
         "## Folds", "", "| horizon | fold | rows | from (UTC) | to (UTC) | days |", "|---|---|---|---|---|---|"]
    for h, folds in res["folds"].items():
        L += [f"| {h}m | {n} | {v['rows']} | {v['from']} | {v['to']} | {v['days']} |" for n, v in folds.items()]
    L += ["", "## Forecast skill against the training base rate", "",
          "Brier difference below zero favors the model. The interval is a 95% day-block bootstrap. `p` is the one-sided bootstrap "
          "share of resamples with no improvement; `p Holm` adjusts it across every test-fold comparison in this report.", "",
          "| horizon | target | fold | model | n | event rate | Brier | Brier - base (95% interval) | skill | AUC | p | p Holm |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in res["cells"]:
        d = "-" if "brier_minus_base" not in c else f"{c['brier_minus_base']:+.5f} ({c['ci_lo']:+.5f} to {c['ci_hi']:+.5f})"
        L.append(f"| {c['horizon_min']}m | {c['target']} | {c['fold']} | {c['model']} | {c['n']} | {c['rate']:.3f} | {c['brier']:.5f} | {d} | "
                 f"{f(c.get('skill'), '+.4f')} | {f(c['auc'], '.3f')} | {f(c.get('p_no_skill'), '.3f')} | {f(c.get('p_holm'), '.3f')} |")
    L += ["", "## Trading the direction forecast, test fold, before costs", "",
          "| horizon | model | trades | hit rate | gross bps per trade (95% interval) |", "|---|---|---|---|---|"]
    L += [f"| {e['horizon_min']}m | {e['model']} | {e['trades']} | {e['hit_rate']:.3f} | {e['gross_bps']:+.2f} ({e['ci_lo']:+.2f} to {e['ci_hi']:+.2f}) |" for e in res["economics"]]
    L += ["", "## Ceiling: move size against round-trip cost", "",
          "Mean absolute move is the gross per trade of perfect directional foresight. The break-even hit rate is (M + C) / 2M for a bet "
          "that wins or loses the mean absolute move M and pays C; above 1.00 no hit rate is enough.", "",
          "Cost scenarios: " + "; ".join(f"{c:g} bps = {label}" for label, c in COSTS_BPS.items()) + ".", "",
          "| symbol | horizon | windows | mean abs move | median | 95th pct | " + " | ".join(f"break-even hit rate at {c:g} bps" for c in COSTS_BPS.values()) + " |",
          "|---|---|---|---|---|---|" + "---|" * len(COSTS_BPS)]
    for r in res["ceiling"]:
        L.append(f"| {r['symbol']} | {r['horizon_min']}m | {r['n']} | {r['mean_abs_bps']:.1f} | {r['median_abs_bps']:.1f} | {r['p95_abs_bps']:.1f} | "
                 + " | ".join((lambda v: "impossible" if v > 1 else f"{v:.3f}")(r[f"breakeven_hit_rate_{c:g}"]) for c in COSTS_BPS.values()) + " |")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="reports/out")
    ap.add_argument("--n-boot", type=int, default=2000)
    args = ap.parse_args(argv)
    data = Path(args.data)
    manifest = json.loads((data / "manifest.json").read_text())
    res = study(data, tuple(manifest["files"]), n_boot=args.n_boot)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "baseline_study.json").write_text(json.dumps({"manifest": manifest, **res}, indent=1) + "\n")
    (out / "baseline_study.md").write_text(render(res, manifest))
    print(f"wrote {out / 'baseline_study.md'} and {out / 'baseline_study.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
