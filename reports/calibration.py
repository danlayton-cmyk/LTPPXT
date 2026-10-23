"""Calibration report: Jev's predicted probabilities vs what happened.

    python -m reports.calibration --logs logs/data --out reports/out

Joins each `answer` event to its `outcome` event by decision_id. Every resolvable
question becomes one or more binary (probability, happened) pairs:
  Noul   -> (noul, outcome)
  Choice -> (p[label], outcome == label) for every label
  Score  -> (p[level], outcome == level) for every level
Pairs are grouped by exact model version and battery version, never pooled across either:
a new battery version means the questions or their resolvers changed.
Writes calibration.md (bucket table, Brier scores, per-label rates) and reliability.svg.
"""

import argparse
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
            if a["type"] == "noul":
                pairs.append(Pair(ans["model"], battery, name, "yes", float(a["noul"]), bool(realized)))
            else:
                for label, p in a["probabilities"].items():
                    pairs.append(Pair(ans["model"], battery, name, str(label), float(p), str(label) == str(realized)))
    return pairs


def brier(pairs: list[Pair]) -> float | None:
    return sum((p.p - p.happened) ** 2 for p in pairs) / len(pairs) if pairs else None


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
    by_group: dict[tuple[str, str], list[Pair]] = defaultdict(list)
    for p in pairs:
        by_group[(p.model, p.battery)].append(p)
    lines = ["# Calibration report", ""]
    if not pairs:
        lines.append("No resolved predictions yet.")
    table_rows = {}
    for (model, battery), gp in sorted(by_group.items()):
        rows = buckets(gp)
        table_rows[f"{model}, {battery}"] = rows
        lines += [f"## {model}, {battery}", "", f"Pairs: {len(gp)}. Brier score: {brier(gp):.4f} (0 is perfect; always answering 0.5 scores 0.25).", "",
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
