"""Daily summary: decisions, trades, PnL after fees, and every risk veto that fired.

    python -m reports.daily --logs logs/data --day 2026-10-17
"""

import argparse
from collections import Counter
from pathlib import Path

from logs.writer import read_events


def summarize(events: list[dict]) -> str:
    decisions = [e for e in events if e["type"] == "decision"]
    orders = [e for e in events if e["type"] == "order" and not e.get("cancelled")]
    fills = [e for e in events if e["type"] == "fill"]
    vetoes = [e for e in events if e["type"] == "veto"]
    rejections = [e for e in events if e["type"] == "rejection"]
    errors = [e for e in events if e["type"] == "error"]
    equity = [e for e in events if e["type"] == "equity"]
    closed = [e for e in events if e["type"] == "trade_closed"]

    fees = sum(f["fee_usd"] + f["gas_usd"] for f in fills)
    if equity:
        pnl = equity[-1]["equity_usd"] - (equity[0]["equity_usd"] - equity[0]["day_pnl_usd"])
        pnl_line = f"PnL after fees and gas (mark-to-market): ${pnl:,.2f}"
    else:
        pnl_line = "PnL after fees and gas: no equity records"
    modes = sorted({e.get("mode") for e in equity if e.get("mode")})

    lines = ["# Daily summary", ""]
    lines.append(f"Mode(s): {', '.join(modes) or 'unknown'}")
    lines.append(f"Decisions made: {len(decisions)} ({', '.join(f'{a}: {n}' for a, n in sorted(Counter(d['action'] for d in decisions).items())) or 'none'})")
    lines.append(f"Orders: {len(orders)} ({sum(1 for o in orders if o.get('executed'))} executed, {sum(1 for o in orders if not o.get('executed'))} not executed)")
    lines.append(f"Fills: {len(fills)}; round trips closed: {len(closed)}")
    lines.append(f"Fees + gas paid: ${fees:,.2f}")
    lines.append(pnl_line)
    lines.append(f"Judge errors: {len(errors)} ({', '.join(f'{c}: {n}' for c, n in sorted(Counter(e['check'] for e in errors).items())) or 'none'})")
    lines.append("")
    lines.append(f"## Risk vetoes ({len(vetoes)})")
    lines.append("")
    if vetoes:
        lines += ["| time | symbol | check | detail | mode |", "|---|---|---|---|---|"]
        lines += [f"| {v['ts']:.0f} | {v['symbol']} | {v['check']} | {v['detail']} | {v.get('mode')} |" for v in vetoes]
    else:
        lines.append("None fired.")
    lines += ["", "## Funnel rejections by check", ""]
    counts = Counter((r["stage"], r["check"].split(":")[0]) for r in rejections)
    lines += [f"- {stage} / {check}: {n}" for (stage, check), n in sorted(counts.items())] or ["None."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default="logs/data")
    ap.add_argument("--day", required=True, help="UTC date, YYYY-MM-DD")
    ap.add_argument("--out", default="reports/out")
    args = ap.parse_args(argv)
    md = summarize(list(read_events(args.logs, args.day)))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"daily-{args.day}.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
