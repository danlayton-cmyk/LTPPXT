"""Daily summary: decisions, trades, PnL after fees, every risk veto that fired, and shadow
mode's hypothetical trades (simulated by desk/hypothetical.py, never real).

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
    stops = [e for e in events if e["type"] == "stop_loss"]
    feed_errors = [e for e in events if e["type"] == "feed_error"]

    fees = sum(f["fee_usd"] + f["gas_usd"] for f in fills)
    modes = sorted({e.get("mode") for e in equity if e.get("mode")})
    if equity and modes == ["shadow"]:
        pnl_line = "PnL: none, shadow mode executes nothing (see Hypothetical trades below)"
    elif equity:
        pnl = equity[-1]["equity_usd"] - (equity[0]["equity_usd"] - equity[0]["day_pnl_usd"])
        pnl_line = f"Paper (simulated) PnL after fees and gas (mark-to-market): ${pnl:,.2f}"
    else:
        pnl_line = "PnL after fees and gas: no equity records"

    lines = ["# Daily summary", ""]
    lines.append(f"Mode(s): {', '.join(modes) or 'unknown'}")
    lines.append(f"Decisions made: {len(decisions)} ({', '.join(f'{a}: {n}' for a, n in sorted(Counter(d['action'] for d in decisions).items())) or 'none'})")
    lines.append(f"Orders: {len(orders)} ({sum(1 for o in orders if o.get('executed'))} executed, {sum(1 for o in orders if not o.get('executed'))} not executed)")
    lines.append(f"Fills: {len(fills)}; round trips closed: {len(closed)} ({sum(1 for c in closed if c.get('exit_reason') == 'stop_loss')} by stop-loss)")
    lines.append(f"Stop-loss triggers: {len(stops)}")
    lines.append(f"Fees + gas paid: ${fees:,.2f}")
    lines.append(pnl_line)
    lines.append(f"Market data errors (throttled to 1/min/symbol): {len(feed_errors)}")
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
    lines += hypothetical_lines(events, shadow="shadow" in modes)
    return "\n".join(lines) + "\n"


def hypothetical_lines(events: list[dict], shadow: bool) -> list[str]:
    """Shadow mode's would-be trades as desk/hypothetical.py filled and closed them. Simulated:
    nothing executed. A trade counts on the day it closes."""
    orders = [e for e in events if e["type"] == "hypothetical_order"]
    fills = [e for e in events if e["type"] == "hypothetical_fill"]
    closed = [e for e in events if e["type"] == "hypothetical_trade_closed"]
    errors = [e for e in events if e["type"] == "hypothetical_error"]
    title = ["", "## Hypothetical trades (simulated, not real)", ""]
    if not (orders or fills or closed or errors):
        return title + ["None logged: no would-be entry passed risk, or this log predates the hypothetical ledger."] if shadow else []

    entries = [o for o in orders if o["role"] == "entry"]
    status = Counter(o["status"] for o in entries)
    skipped = Counter(o["reason"] for o in entries if o["status"] == "skipped")
    exit_vetoes = sum(1 for o in orders if o["role"] == "exit" and o["status"] == "vetoed")
    unclosed = {f["decision_id"] for f in fills if f["role"] == "entry"} - {c["decision_id"] for c in closed}
    net, gross, costs = (sum(c[k] for c in closed) for k in ("net_pnl_usd", "gross_pnl_usd", "costs_usd"))
    wins = sum(1 for c in closed if c["net_pnl_usd"] > 0)
    fmt = lambda counter: ", ".join(f"{k}: {n}" for k, n in sorted(counter.items())) or "none"

    lines = title + [
        "Shadow mode's would-be entries, filled on a simulated paper broker and closed by the stop-loss or the bot's holding period. "
        "Nothing executed: no real orders, no real money.",
        "",
        f"Would-be entries offered: {sum(status[s] for s in ('filled', 'resting', 'skipped', 'vetoed'))}. Taken: {status['filled'] + status['resting']}. "
        f"Skipped: {status['skipped']} ({fmt(skipped)}). Vetoed on hypothetical equity: {status['vetoed']}.",
        f"Entries filled: {sum(1 for f in fills if f['role'] == 'entry')}. Resting limits cancelled unfilled: {status['cancelled']}.",
        f"Round trips closed: {len(closed)} ({fmt(Counter(c['exit_reason'] for c in closed))}). Wins / losses after costs: {wins} / {len(closed) - wins}.",
    ]
    if closed:
        bps = lambda key: sum(c[key] / c["notional_usd"] * 1e4 for c in closed) / len(closed)
        lines.append(f"Hypothetical PnL after fees and gas: ${net:,.2f} (gross ${gross:,.2f} at simulated fill prices, fees and gas ${costs:,.2f})")
        lines.append(f"Mean per trade: gross {bps('gross_pnl_usd'):+.2f} bps, fees and gas {bps('costs_usd'):.2f} bps, net {bps('net_pnl_usd'):+.2f} bps")
    else:
        lines.append("Hypothetical PnL: none yet, no round trip closed")
    if unclosed:
        lines.append(f"Filled entries with no close in this log: {len(unclosed)} (still open at day end, or dropped by a desk restart)")
    lines.append(f"Exits vetoed by risk (retried next block): {exit_vetoes}. Ledger errors: {len(errors)}.")
    if closed:
        by: dict[tuple[str, str], list[dict]] = {}
        for c in closed:
            by.setdefault((c["symbol"], "long" if c["side"] == "buy" else "short"), []).append(c)
        lines += ["", "| symbol | side | trades | wins | hypothetical net PnL | mean net bps |", "|---|---|---|---|---|---|"]
        for (sym, side), cs in sorted(by.items()):
            lines.append(f"| {sym} | {side} | {len(cs)} | {sum(1 for c in cs if c['net_pnl_usd'] > 0)} | "
                         f"${sum(c['net_pnl_usd'] for c in cs):,.2f} | {sum(c['net_bps'] for c in cs) / len(cs):+.2f} |")
    lines += ["", "Simulated fills follow the paper broker: market orders cross the spread plus the configured slippage, and a resting "
              "limit fills at its price only once the far side of the book reaches it, so queue position is ignored. Real fills can be worse."]
    return lines


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
