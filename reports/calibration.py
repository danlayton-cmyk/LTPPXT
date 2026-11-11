"""Calibration report: Jev's predicted probabilities vs what happened.

    python -m reports.calibration --logs logs/data --out reports/out

Joins each `answer` event to its `outcome` event by decision_id. Every resolvable
question becomes one or more binary (probability, happened) pairs:
  Noul   -> (noul, outcome)
  Choice -> (p[label], outcome == label) for every label
  Score  -> (p[level], outcome == level) for every level
Pairs are grouped by exact model version, battery version and source, never pooled:
a new battery version means the questions or their resolvers changed, and the source
(funnel: symbols that passed the filters; schedule: the fixed-rotation sampler; exit:
questions asked while holding) changes which moments were asked about.
Each group also gets a skill section: Brier against two no-skill forecasters (Jev's own
average answer per label, and the hindsight base rate), AUC per label with a 95%
interval, observed rate by fifth of predicted, and a count of non-overlapping windows.
Writes calibration.md (bucket table, Brier scores, per-label rates) and reliability.svg.
"""

import argparse
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from desk.config import OFFLINE_FAKE_MODEL
from logs.writer import read_events

N_BUCKETS = 10


@dataclass(frozen=True)
class Pair:
    model: str
    battery: str  # battery version, e.g. "house-v2"
    question: str  # "<battery_version>.<question_id>"
    label: str  # Choice label or Score level; "yes" for a Noul
    p: float
    happened: bool
    source: str = "funnel"  # funnel | schedule | exit
    decision_id: str = ""
    ts: float | None = None  # when Jev answered
    outcome_ts: float | None = None  # when the outcome resolved


def source_of(answer: dict) -> str:
    """Events written before the source field existed are classified by their decision_id."""
    if answer.get("source"):
        return str(answer["source"])
    did = str(answer.get("decision_id", ""))
    if "-exit-" in did:
        return "exit"
    if "-sample-" in did:
        return "schedule"
    return "funnel"


def collect_pairs(events, include_fake: bool = False) -> list[Pair]:
    answers, outcomes = {}, {}
    for e in events:
        if e["type"] == "answer":
            answers[e["decision_id"]] = e
        elif e["type"] == "outcome":
            outcomes[e["decision_id"]] = e
    pairs = []
    for did, out in outcomes.items():
        ans = answers.get(did)
        if ans is None or (ans["model"] == OFFLINE_FAKE_MODEL and not include_fake):
            continue
        for qid, realized in out["outcomes"].items():
            if realized is None or qid not in ans["answers"]:
                continue
            a, battery = ans["answers"][qid], ans["battery_version"]
            name = f"{battery}.{qid}"
            meta = dict(source=source_of(ans), decision_id=did, ts=ans.get("ts"), outcome_ts=out.get("ts"))
            if a["type"] == "noul":
                pairs.append(Pair(ans["model"], battery, name, "yes", float(a["noul"]), bool(realized), **meta))
            else:
                for label, p in a["probabilities"].items():
                    pairs.append(Pair(ans["model"], battery, name, str(label), float(p), str(label) == str(realized), **meta))
    return pairs


def brier(pairs: list[Pair]) -> float | None:
    return sum((p.p - p.happened) ** 2 for p in pairs) / len(pairs) if pairs else None


def brier_vs(pairs: list[Pair], forecast: dict[tuple[str, str], float]) -> float | None:
    """Brier of a forecaster that answers one fixed number per (question, label)."""
    return sum((forecast[(p.question, p.label)] - p.happened) ** 2 for p in pairs) / len(pairs) if pairs else None


def per_label_constant(pairs: list[Pair], use_outcome: bool) -> dict[tuple[str, str], float]:
    groups: dict[tuple[str, str], list[Pair]] = defaultdict(list)
    for p in pairs:
        groups[(p.question, p.label)].append(p)
    return {k: sum((p.happened if use_outcome else p.p) for p in v) / len(v) for k, v in groups.items()}


def skill(model_brier: float | None, base_brier: float | None) -> float | None:
    if model_brier is None or not base_brier:
        return None
    return 1 - model_brier / base_brier


def auc(pairs: list[Pair]) -> tuple[float, float, float] | None:
    """Mann-Whitney AUC with midranks for ties, and a Hanley-McNeil 95% interval."""
    pos = sum(p.happened for p in pairs)
    neg = len(pairs) - pos
    if not pos or not neg:
        return None
    ordered = sorted(pairs, key=lambda p: p.p)
    rank_sum, i = 0.0, 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1].p == ordered[i].p:
            j += 1
        mid = (i + j) / 2 + 1
        rank_sum += mid * sum(ordered[k].happened for k in range(i, j + 1))
        i = j + 1
    a = (rank_sum - pos * (pos + 1) / 2) / (pos * neg)
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    var = (a * (1 - a) + (pos - 1) * (q1 - a * a) + (neg - 1) * (q2 - a * a)) / (pos * neg)
    half = 1.96 * math.sqrt(max(var, 0.0))
    return a, max(0.0, a - half), min(1.0, a + half)


def fifths(pairs: list[Pair]) -> list[tuple[int, float, float]]:
    """(n, mean predicted, observed) for each fifth of the pairs ranked by predicted probability."""
    ordered = sorted(pairs, key=lambda p: p.p)
    out = []
    for k in range(5):
        chunk = ordered[k * len(ordered) // 5:(k + 1) * len(ordered) // 5]
        if chunk:
            out.append((len(chunk), sum(p.p for p in chunk) / len(chunk), sum(p.happened for p in chunk) / len(chunk)))
    return out


def independent_windows(pairs: list[Pair]) -> int:
    """Greedy count of decisions whose answer-to-outcome windows do not overlap: a rough count of independent evidence."""
    spans = {}
    for p in pairs:
        if p.ts is not None and p.outcome_ts is not None:
            spans[p.decision_id] = (float(p.ts), float(p.outcome_ts))
    count, free_from = 0, -math.inf
    for start, end in sorted(spans.values(), key=lambda s: s[1]):
        if start >= free_from:
            count, free_from = count + 1, end
    return count


def _f(x: float | None, fmt: str = ".4f") -> str:
    return "-" if x is None else format(x, fmt)


def skill_lines(gp: list[Pair]) -> list[str]:
    scored = [p for p in gp if p.label != "other"]
    other = [p for p in gp if p.label == "other"]
    own_mean = per_label_constant(scored, use_outcome=False)
    base_rate = per_label_constant(scored, use_outcome=True)
    jev, mean_b, base_b = brier(scored), brier_vs(scored, own_mean), brier_vs(scored, base_rate)
    lines = ["### Skill", "",
             f"Decisions: {len({p.decision_id for p in gp})}. Non-overlapping outcome windows: {independent_windows(gp)} (a rough count of independent evidence).",
             "",
             "Scored without the \"other\" label, which almost never happens and flatters every forecaster.",
             "",
             "| forecaster | pairs | Brier | skill vs base rate |", "|---|---|---|---|",
             f"| Jev | {len(scored)} | {_f(jev)} | {_f(skill(jev, base_b), '+.3f')} |",
             f"| Jev's own average answer per label | {len(scored)} | {_f(mean_b)} | {_f(skill(mean_b, base_b), '+.3f')} |",
             f"| hindsight base rate per label | {len(scored)} | {_f(base_b)} | +0.000 |"]
    if other:
        lines.append(f"| Jev, \"other\" label only | {len(other)} | {_f(brier(other))} | - |")
    lines += ["", "Skill = 1 - Brier / base-rate Brier; above 0 beats the base rate. The base rate is computed from the same outcomes, so it is a slightly unfair (optimistic) benchmark to beat.",
              "", "| question | label | pairs | happened | AUC | 95% interval | observed rate by fifth of predicted (mean predicted -> observed) |",
              "|---|---|---|---|---|---|---|"]
    by_label: dict[tuple[str, str], list[Pair]] = defaultdict(list)
    for p in scored:
        by_label[(p.question, p.label)].append(p)
    for (q, label), v in sorted(by_label.items()):
        a = auc(v)
        a_s, ci = ("-", "-") if a is None else (f"{a[0]:.3f}", f"{a[1]:.3f}-{a[2]:.3f}")
        fs = "; ".join(f"{mp:.2f}->{ob:.2f}" for _, mp, ob in fifths(v))
        lines.append(f"| {q} | {label} | {len(v)} | {sum(p.happened for p in v)} | {a_s} | {ci} | {fs} |")
    lines += ["", "AUC 0.5 is no ranking skill. The interval treats pairs as independent, so it is too narrow when windows overlap.", ""]
    return lines


def buckets(pairs: list[Pair], n: int = N_BUCKETS) -> list[dict]:
    rows = []
    for i in range(n):
        lo, hi = i / n, (i + 1) / n
        inside = [p for p in pairs if lo <= p.p < hi or (i == n - 1 and p.p == 1.0)]
        rows.append({
            "lo": lo, "hi": hi, "n": len(inside),
            "mean_p": sum(p.p for p in inside) / len(inside) if inside else None,
            "freq": sum(p.happened for p in inside) / len(inside) if inside else None,
        })
    return rows


def reliability_svg(by_group: dict[str, list[dict]]) -> str:
    w = h = 360
    pad = 40
    span = w - 2 * pad

    def xy(px, py):
        return pad + px * span, h - pad - py * span

    colors = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="sans-serif" font-size="11">',
        f'<rect width="{w}" height="{h}" fill="white"/>',
        f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{pad}" stroke="#999" stroke-dasharray="4 4"/>',
        f'<rect x="{pad}" y="{pad}" width="{span}" height="{span}" fill="none" stroke="#333"/>',
        f'<text x="{w / 2}" y="{h - 8}" text-anchor="middle">predicted probability</text>',
        f'<text x="12" y="{h / 2}" text-anchor="middle" transform="rotate(-90 12 {h / 2})">observed frequency</text>',
    ]
    for k, (group, rows) in enumerate(sorted(by_group.items())):
        c = colors[k % len(colors)]
        pts = [xy(r["mean_p"], r["freq"]) for r in rows if r["n"]]
        if len(pts) > 1:
            parts.append(f'<polyline fill="none" stroke="{c}" stroke-width="1.5" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}"/>')
        for r in rows:
            if r["n"]:
                x, y = xy(r["mean_p"], r["freq"])
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{2 + min(6, r["n"] ** 0.5 / 3):.1f}" fill="{c}"/>')
        parts.append(f'<text x="{pad + 6}" y="{pad + 14 + 14 * k}" fill="{c}">{group}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def render(pairs: list[Pair]) -> tuple[str, str]:
    by_group: dict[tuple[str, str, str], list[Pair]] = defaultdict(list)
    for p in pairs:
        by_group[(p.model, p.battery, p.source)].append(p)
    lines = ["# Calibration report", ""]
    if not pairs:
        lines.append("No resolved predictions yet.")
    table_rows = {}
    for (model, battery, source), gp in sorted(by_group.items()):
        rows = buckets(gp)
        title = f"{model}, {battery}" + ("" if source == "funnel" else f", {source}")
        table_rows[title] = rows
        lines += [f"## {title}", "", f"Pairs: {len(gp)}. Brier score: {brier(gp):.4f} (0 is perfect; always answering 0.5 scores 0.25).", "",
                  "| bucket | n | mean predicted | observed |", "|---|---|---|---|"]
        for r in rows:
            mp_, fq = ("-" if r["mean_p"] is None else f"{r['mean_p']:.3f}"), ("-" if r["freq"] is None else f"{r['freq']:.3f}")
            lines.append(f"| {r['lo']:.1f}-{r['hi']:.1f} | {r['n']} | {mp_} | {fq} |")
        by_q: dict[str, list[Pair]] = defaultdict(list)
        by_label: dict[tuple[str, str], list[Pair]] = defaultdict(list)
        for p in gp:
            by_q[p.question].append(p)
            by_label[(p.question, p.label)].append(p)
        lines += ["", "| question | pairs | Brier |", "|---|---|---|"]
        lines += [f"| {q} | {len(v)} | {brier(v):.4f} |" for q, v in sorted(by_q.items())]
        # A label priced far from how often it happens stands out here; the bucket table mixes it with every other label.
        lines += ["", "| question | label | pairs | mean predicted | observed |", "|---|---|---|---|---|"]
        lines += [f"| {q} | {label} | {len(v)} | {sum(p.p for p in v) / len(v):.3f} | {sum(p.happened for p in v) / len(v):.3f} |"
                  for (q, label), v in sorted(by_label.items())]
        lines.append("")
        lines += skill_lines(gp)
    lines.append("Note: pairs within one decision are correlated (a Choice yields one pair per label), so treat bucket counts as an upper bound on independent evidence.")
    return "\n".join(lines) + "\n", reliability_svg(table_rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default="logs/data")
    ap.add_argument("--out", default="reports/out")
    ap.add_argument("--include-fake", action="store_true", help="include offline fake-judge answers (for smoke tests only)")
    args = ap.parse_args(argv)
    md, svg = render(collect_pairs(read_events(args.logs), args.include_fake))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "calibration.md").write_text(md)
    (out / "reliability.svg").write_text(svg)
    print(f"wrote {out / 'calibration.md'} and {out / 'reliability.svg'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
