"""Copy the desk's JSONL event logs into a BigQuery table, one day per partition.

    python -m cloud.export_bq --logs /var/lib/ltppxt/logs --project my-proj            # today + yesterday (UTC)
    python -m cloud.export_bq --logs /var/lib/ltppxt/logs --project my-proj --all      # every file
    python -m cloud.export_bq --logs logs/data --day 2025-10-09 --dry-run --out /tmp/x  # build rows only

How it writes, and why:
- Each run rebuilds whole days and loads them with `bq load --replace` into the day's
  partition (`events$YYYYMMDD`). Re-running is safe: a day is replaced, never duplicated.
- It uses load jobs, which BigQuery does not charge for. It does not use streaming inserts,
  which are billed and are not allowed in the free tier.
- It shells out to the `bq` CLI that Google's Debian images ship with, so the desk stays
  standard-library only and the VM's service account supplies credentials. No key files.
- The log writer already scrubs secret-looking keys before anything reaches disk; this
  module copies those scrubbed records as they are.
- A line still being written (no trailing newline, or bad JSON) is skipped and counted;
  the next run picks it up.

It reads the log files and never writes to them. Nothing in the desk imports this module.
"""

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.json")
TABLE = "events"
TOP_LEVEL = ("bot", "symbol", "decision_id", "model", "mode", "action")


@dataclass
class DayRows:
    day: str
    rows: list[dict] = field(default_factory=list)
    skipped_partial: int = 0
    skipped_other_day: int = 0


def _float(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def to_row(record: dict, source_file: str, line_no: int) -> dict:
    """One BigQuery row per log record. Common fields become columns; the rest stays in payload."""
    ts = float(record["ts"])
    when = datetime.fromtimestamp(ts, UTC)
    row = {
        "day": when.strftime("%Y-%m-%d"),
        "ts": when.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "type": str(record["type"]),
        "p": _float(record.get("p")),
        "net_pnl_usd": _float(record.get("net_pnl_usd")),
        "gross_pnl_usd": _float(record.get("gross_pnl_usd")),
        "payload": json.dumps(record, separators=(",", ":"), sort_keys=True, default=str),
        "source_file": source_file,
        "line_no": line_no,
    }
    for k in TOP_LEVEL:
        v = record.get(k)
        row[k] = None if v is None else str(v)
    return row


def read_day(log_dir: Path, day: str) -> DayRows:
    out = DayRows(day)
    path = log_dir / f"{day}.jsonl"
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    complete = lines[:-1]  # anything after the last newline is a line still being written
    if lines[-1].strip():
        out.skipped_partial += 1
    for i, line in enumerate(complete, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            row = to_row(record, path.name, i)
        except (ValueError, KeyError, TypeError, OverflowError, OSError):
            out.skipped_partial += 1
            continue
        if row["day"] != day:  # the writer files by UTC day of ts, so this should not happen
            out.skipped_other_day += 1
            continue
        out.rows.append(row)
    return out


def write_ndjson(rows: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def bq_load_command(project: str, dataset: str, day: str, data_file: Path, location: str | None) -> list[str]:
    cmd = ["bq", "--quiet", "--headless", f"--project_id={project}"]
    if location:
        cmd.append(f"--location={location}")
    cmd += [
        "load",
        "--replace",
        "--source_format=NEWLINE_DELIMITED_JSON",
        f"--schema={SCHEMA_PATH}",
        f"{project}:{dataset}.{TABLE}${day.replace('-', '')}",
        str(data_file),
    ]
    return cmd


def days_to_export(log_dir: Path, all_days: bool, day: str | None, now: datetime) -> list[str]:
    if day:
        return [day]
    if all_days:
        return sorted(p.stem for p in log_dir.glob("*.jsonl"))
    today = now.astimezone(UTC).date()
    return [(today - timedelta(days=1)).isoformat(), today.isoformat()]


def main(argv: list[str] | None = None, now: datetime | None = None, run=subprocess.run) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--logs", default="logs/data", help="directory of YYYY-MM-DD.jsonl files")
    ap.add_argument("--project", help="Google Cloud project id (required unless --dry-run)")
    ap.add_argument("--dataset", default="ltppxt")
    ap.add_argument("--location", default=None, help="BigQuery location, e.g. US; default lets bq decide")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--day", help="one UTC day, YYYY-MM-DD")
    g.add_argument("--all", action="store_true", help="every file in --logs (backfill)")
    ap.add_argument("--dry-run", action="store_true", help="build the files, print the bq commands, load nothing")
    ap.add_argument("--out", help="with --dry-run, keep the built files here")
    args = ap.parse_args(argv)

    if not args.dry_run and not args.project:
        ap.error("--project is required unless --dry-run")
    if not args.dry_run and shutil.which("bq") is None:
        print("export_bq: the bq CLI is not installed (it ships with the Google Cloud CLI)", file=sys.stderr)
        return 2

    log_dir = Path(args.logs)
    failures = 0
    with tempfile.TemporaryDirectory(prefix="ltppxt-bq-") as tmp:
        out_dir = Path(args.out) if (args.dry_run and args.out) else Path(tmp)
        out_dir.mkdir(parents=True, exist_ok=True)
        for day in days_to_export(log_dir, args.all, args.day, now or datetime.now(UTC)):
            d = read_day(log_dir, day)
            note = f"{day}: {len(d.rows)} rows"
            if d.skipped_partial or d.skipped_other_day:
                note += f" (skipped {d.skipped_partial} partial/bad, {d.skipped_other_day} other-day)"
            if not d.rows:
                # Never replace a partition with nothing: a missing or unreadable file must not wipe data.
                print(f"{note}; nothing to load")
                continue
            data_file = out_dir / f"{day}.ndjson"
            write_ndjson(d.rows, data_file)
            cmd = bq_load_command(args.project or "PROJECT", args.dataset, day, data_file, args.location)
            if args.dry_run:
                print(f"{note}; would run: {' '.join(cmd)}")
                continue
            res = run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                failures += 1
                print(f"{note}; bq load FAILED: {(res.stderr or res.stdout).strip()[:500]}", file=sys.stderr)
            else:
                print(f"{note}; loaded")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
