"""Replay logged house-v2 snapshots: does Jev beat cheap baselines on the same moments?

    python -m reports.replay --logs logs/data --out reports/out                # dry run: logged answers only
    python -m reports.replay --logs logs/data --out reports/out --call-jev    # also ask Jev again (costs calls)

Takes every logged house-v2 question that has a resolved outcome, keeps only decisions
whose answer-to-outcome windows do not overlap (so the samples are closer to independent),
and splits them by time: the oldest two thirds train the baselines, the newest third is
held out and scored. Forecasters, each scored on the same held-out rows:

  base rate          the training-set frequency of each target
  logistic           L2-regularized logistic regression on the logged state (spread, book
                     imbalance, realized vol, returns), fit on the training rows
  jev-1.13.0 logged  the answers the live desk got at the time
  jev-1.14.0 replay  the same house-v2 questions asked again of jev-1.14.0 (--call-jev)
  v3 candidate       jev-1.13.0 asked a reworded battery (big_move, up_move) (--call-jev)

Targets: big_move (|move| > band), bid / ask / neither (quote_lean's resolver), up (mid rose).
Replayed answers are cached in replay-answers.jsonl next to the report, so a rerun does
not pay for the same call twice. Nothing here trades or changes what the desk does.
"""

import argparse
import json
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from logs.writer import read_events
from questions.battery import Battery, band_bps, horizon_text, noul
from reports.calibration import Pair, auc, source_of

BATTERY = "house-v2"
LOGGED_MODEL = "jev-1.13.0"
REPLAY_MODEL = "jev-1.14.0"
HORIZON_S = 300.0
TARGETS = ("big_move", "bid", "ask", "neither", "up")


def v3_candidate(horizon_s: float = HORIZON_S) -> Battery:
    """Reworded house questions, each one judgment with no trading framing. Not used by the desk."""
    h, b = horizon_text(horizon_s), band_bps(horizon_s)
    return Battery(
        bot="house",
        version="house-v3-candidate",
        questions=(
            noul("big_move", f"Will `mid` move more than {b} bps in either direction over the next {h}?"),
            noul("up_move", f"Will `mid` be higher {h} from now than it is now?"),
        ),
    )


@dataclass
class Row:
    decision_id: str
    ts: float
    outcome_ts: float
    state: dict
    questions: dict
    move_bps: float
    targets: dict[str, bool]
    forecasts: dict[str, dict[str, float]] = field(default_factory=dict)  # forecaster -> target -> p


def jev_to_targets(answers: dict) -> dict[str, float]:
    """Map a battery's raw answers onto the shared targets. Missing questions are left out."""
    out = {}
    if "toxic_flow" in answers:
        out["big_move"] = float(answers["toxic_flow"]["noul"])
    if "big_move" in answers:
        out["big_move"] = float(answers["big_move"]["noul"])
    if "up_move" in answers:
        out["up"] = float(answers["up_move"]["noul"])
    if "quote_lean" in answers:
        probs = answers["quote_lean"]["probabilities"]
        for label in ("bid", "ask", "neither"):
            out[label] = float(probs.get(label, 0.0))
    return out


def load_rows(events) -> list[Row]:
    questions, answers, outcomes = {}, {}, {}
    for e in events:
        did = e.get("decision_id")
        if e["type"] == "question" and e.get("battery_version") == BATTERY:
            questions[did] = e
        elif e["type"] == "answer" and e.get("battery_version") == BATTERY and e.get("model") == LOGGED_MODEL:
            answers[did] = e
        elif e["type"] == "outcome" and e.get("battery_version") == BATTERY:
            outcomes[did] = e
    rows = []
    for did, out in outcomes.items():
        q, a = questions.get(did), answers.get(did)
        if q is None or a is None or source_of(a) == "exit":  # exits are asked while holding: a different population
            continue
        lean, toxic = out["outcomes"].get("quote_lean"), out["outcomes"].get("toxic_flow")
        if lean is None or toxic is None or not out.get("mid_then"):
            continue
        move = (out["mid_now"] - out["mid_then"]) / out["mid_then"] * 1e4
        targets = {"big_move": bool(toxic), "bid": lean == "bid", "ask": lean == "ask", "neither": lean == "neither", "up": move > 0}
        row = Row(did, float(a["ts"]), float(out["ts"]), q["state"], q["questions"], move, targets)
        row.forecasts[f"{LOGGED_MODEL} logged"] = jev_to_targets(a["answers"])
        rows.append(row)
    return rows


def thin(rows: list[Row]) -> list[Row]:
    """Greedy: keep the earliest-ending window, then the next one that starts after it ends."""
    kept, free_from = [], -math.inf
    for r in sorted(rows, key=lambda r: r.outcome_ts):
        if r.ts >= free_from:
            kept.append(r)
            free_from = r.outcome_ts
    return sorted(kept, key=lambda r: r.ts)


def features(state: dict) -> list[float]:
    ret = state.get("return_bps") or {}
    r = [float(ret.get(k) or 0.0) for k in ("1m", "5m", "15m")]
    return [float(state.get("spread_bps") or 0.0), float(state.get("book_imbalance") or 0.0), float(state.get("realized_vol_bps") or 0.0),
            *r, *(abs(x) for x in r)]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; a is small and positive definite here."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


class Logistic:
    """L2-regularized logistic regression fit by Newton's method on standardized features."""

    def __init__(self, l2: float = 1.0, iters: int = 25) -> None:
        self.l2, self.iters = l2, iters
        self.mu: list[float] = []
        self.sd: list[float] = []
        self.w: list[float] = []

    def _x(self, f: list[float]) -> list[float]:
        return [1.0] + [(v - m) / s for v, m, s in zip(f, self.mu, self.sd)]

    def fit(self, xs: list[list[float]], ys: list[bool]) -> "Logistic":
        k = len(xs[0])
        self.mu = [sum(x[j] for x in xs) / len(xs) for j in range(k)]
        self.sd = [max(1e-9, math.sqrt(sum((x[j] - self.mu[j]) ** 2 for x in xs) / len(xs))) for j in range(k)]
        data = [self._x(x) for x in xs]
        w = [0.0] * (k + 1)
        for _ in range(self.iters):
            grad = [0.0] * (k + 1)
            hess = [[0.0] * (k + 1) for _ in range(k + 1)]
            for x, y in zip(data, ys):
                p = 1 / (1 + math.exp(-max(-30.0, min(30.0, sum(a * b for a, b in zip(w, x))))))
                for i in range(k + 1):
                    grad[i] += (p - y) * x[i]
                    for j in range(k + 1):
                        hess[i][j] += p * (1 - p) * x[i] * x[j]
            for i in range(1, k + 1):  # the intercept is not penalized
                grad[i] += self.l2 * w[i]
                hess[i][i] += self.l2
            step = _solve(hess, grad)
            w = [a - b for a, b in zip(w, step)]
            if max(abs(s) for s in step) < 1e-8:
                break
        self.w = w
        return self

    def predict(self, f: list[float]) -> float:
        z = sum(a * b for a, b in zip(self.w, self._x(f)))
        return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def add_baselines(train: list[Row], test: list[Row]) -> None:
    for t in TARGETS:
        rate = sum(r.targets[t] for r in train) / len(train)
        ys = [r.targets[t] for r in train]
        model = Logistic().fit([features(r.state) for r in train], ys) if 0 < sum(ys) < len(ys) else None
        for r in test:
            r.forecasts.setdefault("base rate", {})[t] = rate
            r.forecasts.setdefault("logistic", {})[t] = model.predict(features(r.state)) if model else rate


# -- replays ---------------------------------------------------------------------------

def load_cache(path: Path) -> dict[tuple[str, str, str], dict]:
    cache = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                cache[(e["decision_id"], e["model"], e["battery"])] = e["answers"]
    return cache


def replay(rows: list[Row], plans: list[tuple[str, str, str, dict | None]], cache_path: Path, call: bool, max_calls: int,
           make_client, log=print) -> dict[str, int]:
    """plans: (forecaster name, expected model, battery version, wire questions or None to reuse the logged ones).
    Fills row.forecasts from the cache, asking the judge for missing answers only when `call` is set."""
    cache = load_cache(cache_path)
    calls, missing = 0, defaultdict(int)
    clients = {}
    for name, model, battery, wire in plans:
        for r in rows:
            key = (r.decision_id, model, battery)
            if key not in cache and call and calls < max_calls:
                if model not in clients:
                    clients[model] = make_client(model)
                calls += 1
                res = None
                try:
                    res = clients[model].ask(r.state, wire if wire is not None else r.questions, time.monotonic() + 60)
                except Exception as e:  # one failed call should not lose the rest of the run
                    log(f"{name}: {r.decision_id}: {type(e).__name__}: {e}")
                if res is not None and res.model != model:
                    log(f"{name}: judge answered with {res.model}, expected {model}; not cached")
                elif res is not None:
                    cache[key] = res.answers
                    with open(cache_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"decision_id": r.decision_id, "model": model, "battery": battery, "answers": res.answers}) + "\n")
            if key in cache:
                r.forecasts[name] = jev_to_targets(cache[key])
            else:
                missing[name] += 1
    return {"calls": calls, **{f"missing:{k}": v for k, v in missing.items()}}


# -- scoring ---------------------------------------------------------------------------

def score_rows(test: list[Row]) -> dict[str, dict[str, dict]]:
    """forecaster -> target -> {n, brier, base_brier, skill, auc}. Each forecaster is compared with the base
    rate on exactly the rows it answered."""
    names = sorted({n for r in test for n in r.forecasts})
    out: dict[str, dict[str, dict]] = {}
    for name in names:
        out[name] = {}
        for t in TARGETS:
            rows = [r for r in test if t in r.forecasts.get(name, {})]
            if not rows:
                continue
            b = sum((r.forecasts[name][t] - r.targets[t]) ** 2 for r in rows) / len(rows)
            base = sum((r.forecasts["base rate"][t] - r.targets[t]) ** 2 for r in rows) / len(rows)
            a = auc([Pair("", "", t, "yes", r.forecasts[name][t], r.targets[t]) for r in rows])
            out[name][t] = {"n": len(rows), "brier": b, "base_brier": base, "skill": 1 - b / base if base else None, "auc": a}
    return out


def lean_gross_bps(test: list[Row]) -> dict[str, tuple[int, float]]:
    """Mean gross bps of trading each forecaster's lean on held-out rows, before any fees: long if it
    rates up (or bid) above down (or ask), short otherwise. Uses `up` when the forecaster has it, else bid vs ask."""
    out = {}
    for name in sorted({n for r in test for n in r.forecasts}):
        pnl = []
        for r in test:
            f = r.forecasts.get(name, {})
            if "up" in f and name != "base rate":
                side = 1 if f["up"] > 0.5 else -1
            elif "bid" in f and "ask" in f:
                side = 1 if f["bid"] >= f["ask"] else -1
            else:
                continue
            pnl.append(side * r.move_bps)
        if pnl:
            out[name] = (len(pnl), sum(pnl) / len(pnl))
    return out


def render(all_rows: int, kept: list[Row], train: list[Row], test: list[Row], scores, gross, stats: dict, called: bool) -> str:
    f = lambda x, fmt=".4f": "-" if x is None else format(x, fmt)
    lines = ["# Replay: Jev vs baselines on logged house-v2 moments", "",
             f"Logged house-v2 questions with outcomes (funnel and scheduled, not exits): {all_rows}. "
             f"After dropping overlapping windows: {len(kept)}. Train (oldest 2/3): {len(train)}. Held out (newest 1/3): {len(test)}.",
             "", "Scores are on the held-out rows only. Skill = 1 - Brier / base-rate Brier on the same rows; above 0 beats the base rate.", ""]
    if not called:
        lines += ["Dry run: replayed Jev answers come from the cache only. Run with --call-jev to ask Jev again.", ""]
    lines += [f"Judge calls this run: {stats.get('calls', 0)}. " + ", ".join(f"{k.removeprefix('missing:')} has no answer for {v} rows" for k, v in stats.items() if k.startswith("missing:")), ""]
    lines += ["| forecaster | target | n | Brier | base-rate Brier | skill | AUC (95% interval) |", "|---|---|---|---|---|---|---|"]
    for name, by_t in scores.items():
        for t, s in by_t.items():
            a = "-" if s["auc"] is None else f"{s['auc'][0]:.3f} ({s['auc'][1]:.3f}-{s['auc'][2]:.3f})"
            lines.append(f"| {name} | {t} | {s['n']} | {f(s['brier'])} | {f(s['base_brier'])} | {f(s['skill'], '+.3f')} | {a} |")
    lines += ["", "## Trading each forecaster's lean, before fees", "",
              "Long when the forecaster rates up (or bid) higher, short otherwise; gross move over the holding period. "
              "Coinbase US entry-level fees are 140-180 bps a round trip, so a 5-minute lean would need to clear that to matter.", "",
              "| forecaster | trades | mean gross bps |", "|---|---|---|"]
    lines += [f"| {name} | {n} | {g:+.2f} |" for name, (n, g) in gross.items()]
    lines += ["", f"With {len(test)} held-out rows, an AUC near 0.55 cannot be told apart from 0.5; the earlier sample-size estimate was about 935 independent forecasts."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay logged house-v2 moments through baselines and, optionally, Jev again")
    ap.add_argument("--logs", default="logs/data")
    ap.add_argument("--out", default="reports/out")
    ap.add_argument("--call-jev", action="store_true", help="ask Jev for answers missing from the cache (costs calls)")
    ap.add_argument("--max-calls", type=int, default=200, help="cap on judge calls this run (default 200)")
    ap.add_argument("--judge-url", default="http://127.0.0.1:8787", help=f"judge serving {LOGGED_MODEL}")
    ap.add_argument("--replay-judge-url", default="http://127.0.0.1:8788", help=f"judge serving {REPLAY_MODEL}")
    args = ap.parse_args(argv)

    all_rows = load_rows(read_events(args.logs))
    kept = thin(all_rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if len(kept) < 30:
        (out / "replay.md").write_text(f"# Replay\n\nOnly {len(kept)} non-overlapping house-v2 rows with outcomes; need at least 30.\n")
        print(f"only {len(kept)} usable rows; wrote {out / 'replay.md'}")
        return 0
    cut = len(kept) * 2 // 3
    train, test = kept[:cut], kept[cut:]
    add_baselines(train, test)

    def make_client(model: str):
        from judge.client import JudgeClient
        return JudgeClient(args.replay_judge_url if model == REPLAY_MODEL else args.judge_url, os.environ.get("DESK_JUDGE_SECRET", ""))

    plans = [(f"{REPLAY_MODEL} replay", REPLAY_MODEL, BATTERY, None),
             (f"{LOGGED_MODEL} on house-v3-candidate", LOGGED_MODEL, "house-v3-candidate", v3_candidate().to_wire())]
    stats = replay(test, plans, out / "replay-answers.jsonl", args.call_jev, args.max_calls, make_client)
    md = render(len(all_rows), kept, train, test, score_rows(test), lean_gross_bps(test), stats, args.call_jev)
    (out / "replay.md").write_text(md)
    print(f"wrote {out / 'replay.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
