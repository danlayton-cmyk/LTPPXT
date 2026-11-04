#!/usr/bin/env python3
"""Print a health check of the desk, and bundle all of its data into one archive to download.

On the VM, in an SSH session:

    ltppxt export                 # health check plus archive
    ltppxt health                 # health check only
    curl -fsSL https://raw.githubusercontent.com/danlayton-cmyk/LTPPXT/main/deploy/export.py | sudo python3 -

Read-only for the desk: it stops, restarts and reconfigures nothing, and it runs at the
lowest CPU and disk priority. It writes one .tar.gz to the home directory of the user who
ran sudo, which is where SSH-in-browser's download button looks for a file name.

In the archive:
  data/         all of /var/lib/ltppxt: the event logs (one JSONL file per UTC day) and reports
  journal.txt   the judge's and desk's service output, from the systemd journal
  system/       installed unit files, deployed commit, pip freeze, service state, disk, clock
  health.txt    the health check printed below

Left out: /etc/ltppxt, which holds the TypeSafe key and the desk secret. Only its file names
and sizes are listed. Those values are also removed from journal.txt and system/, and the
export stops without leaving an archive if one turns up in data/. It also stops before the
disk gets low, so the desk can always keep writing its log.

Standard library only, Python 3.11+, so it runs on the VM's system Python. For tests, paths
can be overridden with LTPPXT_DATA, LTPPXT_APP, LTPPXT_ETC, LTPPXT_UNITS and LTPPXT_EXPORT_DIR.
"""

import argparse
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

SYSTEM_DATA = "/var/lib/ltppxt"
DATA = Path(os.environ.get("LTPPXT_DATA", SYSTEM_DATA))
APP = Path(os.environ.get("LTPPXT_APP", "/opt/ltppxt"))
ETC = Path(os.environ.get("LTPPXT_ETC", "/etc/ltppxt"))
UNITS = Path(os.environ.get("LTPPXT_UNITS", "/etc/systemd/system"))
SERVICES = ("ltppxt-judge", "ltppxt-desk")
BLOCK_S = 10.0  # desk/config.py block_interval_s: the desk writes one equity event per block
STALL_S = 60.0  # a longer gap between two blocks counts as a stall
DAYS = 10  # days of logs the health check reads; the archive holds every day
MIN_FREE_BYTES = 500 * 2**20  # stop writing the archive before free disk drops below this
REDACTED = "[redacted]"


# -- small helpers -------------------------------------------------------------------


def out(*cmd: str, timeout: float = 120) -> str:
    """A command's stdout, or "" if it failed or does not exist here."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout if p.returncode == 0 else ""


def capture(*cmd: str, timeout: float = 300) -> str:
    """A command's stdout and stderr for a file in the archive, with a note when it failed."""
    head = "$ " + " ".join(cmd) + "\n"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return head + f"[not run: {type(e).__name__}: {e}]\n"
    return head + p.stdout + p.stderr + (f"[exit {p.returncode}]\n" if p.returncode else "")


def secrets_in(etc: Path) -> list[str]:
    """Values in the env files under /etc/ltppxt: the TypeSafe key and the desk secret."""
    found = set()
    for f in sorted(etc.glob("*.env")) if etc.is_dir() else []:
        for line in f.read_text(errors="replace").splitlines():
            _, sep, value = line.partition("=")
            value = value.strip().strip("'\"")
            if sep and len(value) >= 8:
                found.add(value)
    return sorted(found, key=len, reverse=True)


def scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, REDACTED)
    return text


def when(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S")


def span(seconds: float) -> str:
    s = int(round(seconds))
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} GB"


def short(text: str, n: int = 150) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


def counts(c: Counter) -> str:
    return ", ".join(f"{k}: {v:,}" for k, v in sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))) or "none"


# -- the health check ----------------------------------------------------------------


def read_day(path: Path, bad: list[int]) -> Iterator[dict]:
    """Events in one day's log, one at a time so a large day never sits in memory. Lines that
    can't be read are counted in bad[0]; a line still being written when the file is read is
    cut short, which is normal for the newest file."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                bad[0] += 1


def service_lines() -> list[str]:
    show = out("systemctl", "show", *SERVICES, "--no-pager", "-p", "Id,ActiveState,SubState,ActiveEnterTimestamp,NRestarts")
    units: dict[str, dict[str, str]] = {}
    for block in show.strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if props.get("Id"):
            units[props["Id"].removesuffix(".service")] = props
    lines = ["Services:"]
    for name in SERVICES:
        p = units.get(name)
        if p is None:
            lines.append(f"  {name:<13} unknown (systemctl returned nothing)")
        else:
            lines.append(f"  {name:<13} {p.get('ActiveState', '?')} ({p.get('SubState', '?')}) since {p.get('ActiveEnterTimestamp') or '?'}, "
                         f"automatic restarts: {p.get('NRestarts', '?')}")
    unit = UNITS / "ltppxt-desk.service"
    m = re.search(r"--mode (\S+) --bot (\S+)", unit.read_text(errors="replace")) if unit.is_file() else None
    lines.append(f"  desk runs: {f'{m.group(1)} mode, {m.group(2)} bot' if m else 'unknown (no installed ltppxt-desk.service)'}")
    return lines


def code_line() -> str:
    head = out("git", "-C", str(APP), "log", "-1", "--format=%h %cs %s").strip() or "unknown"
    edits = [ln for ln in out("git", "-C", str(APP), "status", "--porcelain", "--untracked-files=no").splitlines() if ln.strip()]
    hypothetical = "yes" if (APP / "desk" / "hypothetical.py").is_file() else "no"
    local = f", plus {len(edits)} local edits" if edits else ""
    return f"Deployed code: {head}{local}. Hypothetical ledger (PR #6) present: {hypothetical}"


def disk_line(logs: list[Path]) -> str:
    try:
        du = shutil.disk_usage(DATA)
        free = f"{size(du.free)} free of {size(du.total)}"
    except OSError:
        free = "free space unknown"
    total = sum(p.stat().st_size for p in DATA.rglob("*") if p.is_file())
    days = f" in {len(logs)} daily logs, {logs[0].stem} to {logs[-1].stem}" if logs else ", no daily logs"
    return f"Disk: {free}. Desk data: {size(total)}{days}"


def clock_line() -> str:
    synced = out("timedatectl", "show", "-p", "NTPSynchronized", "--value").strip() or "unknown"
    return f"Clock NTP-synchronized: {synced} (the desk measures data age against this clock)"


def log_lines(logs: list[Path], now: float) -> list[str]:
    recent = logs[-DAYS:]
    if not recent:
        return [f"Event log: no daily logs in {DATA / 'logs'}"]
    per_day: dict[str, Counter] = {}
    blocks: list[float] = []
    errors: dict[str, list] = {}  # check -> [count, latest event]
    feed_errors: dict[str, list] = {}  # symbol -> [count, latest event]
    models: Counter = Counter()
    closed: list[dict] = []
    entries: Counter = Counter()
    ledger_errors: list = [0, None]
    bad, last_ts = [0], None
    for path in recent:
        types = per_day[path.stem] = Counter()
        for e in read_day(path, bad):
            t, ts = e.get("type"), e.get("ts")
            types[t] += 1
            if not isinstance(ts, (int, float)):
                continue
            last_ts = ts if last_ts is None else max(last_ts, ts)
            if t == "equity":
                blocks.append(ts)
            elif t == "error":
                seen = errors.setdefault(str(e.get("check")), [0, None])
                seen[0], seen[1] = seen[0] + 1, e
            elif t == "feed_error":
                seen = feed_errors.setdefault(str(e.get("symbol")), [0, None])
                seen[0], seen[1] = seen[0] + 1, e
            elif t == "answer":
                models[str(e.get("model"))] += 1
            elif t == "hypothetical_trade_closed":
                closed.append({k: e.get(k) for k in ("net_pnl_usd", "net_bps", "exit_reason")})
            elif t == "hypothetical_order" and e.get("role") == "entry":
                entries[str(e.get("status"))] += 1
            elif t == "hypothetical_error":
                ledger_errors[0], ledger_errors[1] = ledger_errors[0] + 1, e
    bad_total = bad[0]

    blocks.sort()
    gaps = [(a, b - a) for a, b in zip(blocks, blocks[1:]) if b - a > STALL_S]
    stalls_by_day = Counter(datetime.fromtimestamp(a + g, UTC).strftime("%Y-%m-%d") for a, g in gaps)
    longest_by_day: dict[str, float] = {}
    for a, b in zip(blocks, blocks[1:]):
        d = datetime.fromtimestamp(b, UTC).strftime("%Y-%m-%d")
        longest_by_day[d] = max(longest_by_day.get(d, 0.0), b - a)

    today = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    midnight = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    lines = [f"Event log, last {len(recent)} UTC days. A block is {BLOCK_S:.0f} s, so a full day has {86400 / BLOCK_S:,.0f} blocks "
             f"(today so far: {(now - midnight) / BLOCK_S:,.0f}). A stall is a gap over {STALL_S:.0f} s between blocks.",
             f"  {'day':<10} {'blocks':>7} {'stalls':>6} {'longest':>8} {'starts':>6} {'decisions':>9} {'answers':>7} "
             f"{'errors':>6} {'feed_err':>8} {'hyp_trades':>10}"]
    for day, t in per_day.items():
        lines.append(f"  {day:<10} {t['equity']:>7,} {stalls_by_day[day]:>6,} {span(longest_by_day.get(day, 0.0)):>8} {t['backfill']:>6,} "
                     f"{t['decision']:>9,} {t['answer']:>7,} {t['error']:>6,} {t['feed_error']:>8,} {t['hypothetical_trade_closed']:>10,}")
    lines.append("  (starts = desk starts that backfilled history; hyp_trades = simulated round trips closed)")
    if last_ts is not None:
        lines.append(f"Last event: {when(last_ts)} UTC, {span(max(0.0, now - last_ts))} ago")
    if today not in per_day:
        lines.append(f"No log file for today ({today}) yet")
    if bad_total:
        lines.append(f"Unreadable log lines: {bad_total:,} (one cut-short line in the newest file is normal)")

    if gaps:
        lines.append("Longest stalls: " + "; ".join(f"{when(a)} for {span(g)}" for a, g in sorted(gaps, key=lambda x: -x[1])[:5]))
    else:
        lines.append("Stalls: none")

    lines.append("Errors (error events by check):" if errors else "Errors: none")
    for check, (n, last) in sorted(errors.items(), key=lambda kv: -kv[1][0])[:8]:
        lines.append(f"  {check:<18} {n:>6,}  latest {when(last['ts'])}: {short(last.get('detail', ''))}")
    if feed_errors:
        lines.append("Market data errors by symbol (at most one per symbol per minute):")
        for sym, (n, last) in sorted(feed_errors.items()):
            lines.append(f"  {sym:<10} {n:>6,}  latest {when(last['ts'])}: {short(last.get('detail', ''))}")
    lines.append(f"Jev answers by model: {counts(models)}")

    lines.append("Hypothetical trades, SIMULATED (shadow mode's would-be trades; no real orders, no real money):")
    if closed:
        wins = sum(1 for c in closed if (c["net_pnl_usd"] or 0) > 0)
        net = sum(c["net_pnl_usd"] or 0.0 for c in closed)
        mean_bps = sum(c["net_bps"] or 0.0 for c in closed) / len(closed)
        reasons = Counter(str(c.get("exit_reason")) for c in closed)
        lines.append(f"  round trips closed: {len(closed):,} ({counts(reasons)}); wins after costs: {wins:,}")
        lines.append(f"  hypothetical net P&L: ${net:,.2f}, mean {mean_bps:+.2f} bps per trade, at the repo's default fees (not Coinbase's)")
    else:
        lines.append("  round trips closed: none in these days")
    n_ledger, last_ledger = ledger_errors
    lines.append(f"  would-be entries by status: {counts(entries)}; ledger errors: {n_ledger:,}"
                 + (f", latest: {short(last_ledger.get('detail', ''))}" if last_ledger else ""))
    return lines


def journal_lines(journal: str) -> list[str]:
    stamps = re.findall(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d)", journal, flags=re.M)
    if not stamps:
        return ["Service output (journal): none found"]
    statuses = Counter(re.findall(r"judge status=(\d+)", journal))
    judge_starts = len(re.findall(r"Started (?:ltppxt-judge\.service|LTPPXT judge)", journal))
    desk_starts = len(re.findall(r"Started (?:ltppxt-desk\.service|LTPPXT paper desk)", journal))
    crashes = len(re.findall(r"Main process exited", journal))
    tracebacks = journal.count("Traceback (most recent call last)")
    problem = re.compile(r"Traceback|Exception|Error\b|Main process exited|Failed|refusing|judge status=[45]\d\d")
    worst = [ln for ln in journal.splitlines() if problem.search(ln)]
    lines = [f"Service output (journal, {stamps[0].replace('T', ' ')} to {stamps[-1].replace('T', ' ')} UTC):",
             f"  judge replies by HTTP status: {counts(statuses)}",
             f"  starts: judge {judge_starts}, desk {desk_starts}; unexpected exits: {crashes}; Python tracebacks: {tracebacks}"]
    if worst:
        lines.append(f"  problem lines: {len(worst):,}, latest:")
        lines += [f"    {short(ln, 170)}" for ln in worst[-5:]]
    return lines


def health(now: float, journal: str) -> str:
    logs = sorted((DATA / "logs").glob("*.jsonl"))
    lines = [f"LTPPXT health check, {when(now)} UTC", ""]
    lines += service_lines()
    lines += [code_line(), disk_line(logs), clock_line(), ""]
    lines += log_lines(logs, now)
    lines += [""] + journal_lines(journal)
    return "\n".join(lines) + "\n"


# -- the archive ---------------------------------------------------------------------


class SecretFound(Exception):
    pass


class LowDisk(Exception):
    pass


class GuardedFile:
    """The archive's output file. Stops the write before free disk drops below the floor."""

    CHECK_EVERY = 8 * 2**20

    def __init__(self, path: Path, floor: int) -> None:
        self.path, self.floor = path, floor
        self.unchecked = self.CHECK_EVERY  # check before the first write too
        self.f = open(path, "wb")

    def write(self, b) -> int:
        self.unchecked += len(b)
        if self.unchecked >= self.CHECK_EVERY:
            self.unchecked = 0
            if shutil.disk_usage(self.path.parent).free < self.floor:
                raise LowDisk(f"less than {size(self.floor)} of disk would be left")
        return self.f.write(b)

    def flush(self) -> None:
        self.f.flush()

    def tell(self) -> int:
        return self.f.tell()

    def close(self) -> None:
        self.f.close()


class ScannedFile:
    """A data file read for the archive, checked for every secret as it is read."""

    def __init__(self, path: Path, needles: list[bytes]) -> None:
        self.path, self.needles = path, needles
        self.keep = max((len(n) for n in needles), default=1) - 1
        self.tail = b""
        self.f = open(path, "rb")

    def read(self, n: int = -1) -> bytes:
        b = self.f.read(n)
        window = self.tail + b
        if any(x in window for x in self.needles):
            raise SecretFound(str(self.path))
        self.tail = window[-self.keep:] if self.keep else b""
        return b

    def close(self) -> None:
        self.f.close()


def system_files(secrets: list[str]) -> dict[str, str]:
    listing = sorted(str(p) for p in DATA.iterdir()) if DATA.is_dir() else []
    files = {
        "os.txt": capture("cat", "/etc/os-release") + capture("uname", "-a") + capture("uptime"),
        "disk.txt": capture("df", "-h") + (capture("du", "-sh", *listing) if listing else "") + capture("ls", "-la", str(DATA / "logs")),
        "memory.txt": capture("free", "-m"),
        "clock.txt": capture("timedatectl"),
        "services.txt": capture("systemctl", "status", *SERVICES, "--no-pager", "--lines=0")
        + capture("systemctl", "show", *SERVICES, "--no-pager", "-p",
                  "Id,ActiveState,SubState,Result,NRestarts,ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainStatus"),
        "boots.txt": capture("journalctl", "--list-boots", "--no-pager") + capture("journalctl", "--disk-usage"),
        "code.txt": capture("git", "-C", str(APP), "log", "-10", "--format=%H %cI %s") + capture("git", "-C", str(APP), "status", "--short"),
        "pip-freeze.txt": capture(str(APP / ".venv" / "bin" / "pip"), "freeze"),
        "etc-ltppxt-listing.txt": capture("ls", "-la", str(ETC)) + "Contents not exported: these files hold the TypeSafe key and the desk secret.\n",
    }
    for unit in sorted(UNITS.glob("ltppxt-*.service")):
        files[f"units/{unit.name}"] = unit.read_text(errors="replace")
    return {name: scrub(text, secrets) for name, text in files.items()}


def destination(user: str | None) -> tuple[Path, int | None, int | None, bool]:
    """Where the archive goes, its owner, and whether that is the SSH user's home directory."""
    if d := os.environ.get("LTPPXT_EXPORT_DIR"):
        return Path(d), None, None, False
    user = user or os.environ.get("SUDO_USER")
    if user and user != "root":
        pw = pwd.getpwnam(user)
        return Path(pw.pw_dir), pw.pw_uid, pw.pw_gid, True
    return Path.home(), None, None, True


def write_archive(final: Path, top: str, stage: Path, secrets: list[str]) -> None:
    """Write the archive under a temporary name and rename it when complete, so a stopped
    export never leaves a file that looks whole."""
    part = final.with_name("." + final.name + ".part")
    needles = [s.encode() for s in secrets]
    guard = GuardedFile(part, MIN_FREE_BYTES)
    try:
        with tarfile.open(mode="w:gz", fileobj=guard, compresslevel=6) as tar:
            tar.add(str(stage), arcname=top, recursive=True)
            for p in [DATA, *sorted(DATA.rglob("*"))]:
                arcname = f"{top}/data" if p == DATA else f"{top}/data/{p.relative_to(DATA).as_posix()}"
                info = tar.gettarinfo(str(p), arcname=arcname)
                if info is None:
                    continue  # sockets and the like
                if info.isfile():
                    src = ScannedFile(p, needles)
                    try:
                        tar.addfile(info, src)
                    finally:
                        src.close()
                else:
                    tar.addfile(info)
        guard.close()
        os.replace(part, final)
    except BaseException:
        guard.close()
        part.unlink(missing_ok=True)
        raise


def lower_priority() -> None:
    try:
        os.nice(19)
    except OSError:
        pass
    try:
        subprocess.run(["ionice", "-c", "3", "-p", str(os.getpid())], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LTPPXT health check and data export (read-only for the desk)")
    ap.add_argument("--health-only", action="store_true", help="print the health check, write no archive")
    ap.add_argument("--user", help="put the archive in this user's home directory (default: the user who ran sudo)")
    args = ap.parse_args(argv)

    if str(DATA) == SYSTEM_DATA and os.geteuid() != 0:
        print("export: run it with sudo, it reads root-only logs", file=sys.stderr)
        return 2
    if not DATA.is_dir():
        print(f"export: no desk data at {DATA}", file=sys.stderr)
        return 2
    lower_priority()
    secrets = secrets_in(ETC)
    now = time.time()
    journal = scrub(capture("journalctl", "-u", "ltppxt-judge", "-u", "ltppxt-desk", "--no-pager", "--utc", "-o", "short-iso-precise"), secrets)
    report = scrub(health(now, journal), secrets)
    print(report)
    if args.health_only:
        return 0

    dest, uid, gid, is_home = destination(args.user)
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%MZ")
    top = f"ltppxt-export-{stamp}"
    final = dest / f"{top}.tar.gz"
    print(f"Writing {final} (this can take a few minutes on a small VM)...", flush=True)
    with tempfile.TemporaryDirectory(prefix="ltppxt-export-") as tmp:
        stage = Path(tmp) / "stage"
        (stage / "system" / "units").mkdir(parents=True)
        (stage / "journal.txt").write_text(journal)
        (stage / "health.txt").write_text(report)
        for name, text in system_files(secrets).items():
            (stage / "system" / name).write_text(text)
        try:
            write_archive(final, top, stage, secrets)
        except SecretFound as e:
            print(f"Stopped, no archive written: {e} contains the TypeSafe key or the desk secret. Don't share that "
                  "file. The desk's log writer should have removed it, so this is a bug to look into.", file=sys.stderr)
            return 1
        except LowDisk as e:
            print(f"Stopped, no archive written: {e}. Free some space or download the logs a few days at a time.", file=sys.stderr)
            return 1
    if uid is not None:
        os.chown(final, uid, gid)
    os.chmod(final, 0o600)
    print(f"Archive: {final} ({size(final.stat().st_size)})")
    if is_home:
        print(f"To download it, click the download icon at the top of this SSH-in-browser window, enter {final.name} "
              f"in the Path field, and click Download. Afterwards you can delete it from the VM with: rm ~/{final.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
