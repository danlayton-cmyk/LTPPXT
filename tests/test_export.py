"""deploy/export.py: the health check and the data archive, against a fake VM layout."""

import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "export.py"
KEY = "ts-live-0123456789abcdef"
DESK_SECRET = "d" * 64
DAY0 = datetime(2026, 11, 3, tzinfo=UTC).timestamp()


def load_export():
    spec = importlib.util.spec_from_file_location("ltppxt_export", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_day(path: Path, events: list[dict], cut_short: bool = False) -> None:
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e, separators=(",", ":"), sort_keys=True) + "\n")
        if cut_short:
            f.write('{"type":"equity","ts":17')


def sample_day() -> list[dict]:
    """An hour of blocks with a 5-minute stall, plus errors, answers and hypothetical trades."""
    events = [{"type": "equity", "ts": DAY0 + t, "mode": "shadow"} for t in range(0, 3600, 10) if not 1200 <= t < 1500]
    events += [
        {"type": "backfill", "ts": DAY0 + 1500, "ticks": {"BTC-USD": 17}},
        {"type": "error", "ts": DAY0 + 100, "check": "jev_deadline", "detail": "deadline passed"},
        {"type": "error", "ts": DAY0 + 101, "check": "jev_deadline", "detail": "deadline passed"},
        {"type": "feed_error", "ts": DAY0 + 300, "symbol": "SOL-USD", "detail": "HTTPError 503"},
        {"type": "answer", "ts": DAY0 + 400, "model": "jev-1.13.0"},
        {"type": "decision", "ts": DAY0 + 400, "action": "enter_long"},
        {"type": "hypothetical_order", "ts": DAY0 + 400, "role": "entry", "status": "filled"},
        {"type": "hypothetical_order", "ts": DAY0 + 410, "role": "entry", "status": "skipped", "reason": "position_open"},
        {"type": "hypothetical_trade_closed", "ts": DAY0 + 700, "net_pnl_usd": -0.25, "net_bps": -10.0, "exit_reason": "holding_period"},
        {"type": "hypothetical_trade_closed", "ts": DAY0 + 900, "net_pnl_usd": 0.5, "net_bps": 20.0, "exit_reason": "stop_loss"},
    ]
    return sorted(events, key=lambda e: e["ts"])


class FakeVM:
    """Data, config and unit directories laid out like the VM's, plus a journalctl that prints a secret."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data, self.etc, self.units, self.out, self.bin = (root / n for n in ("data", "etc", "units", "out", "bin"))
        for d in (self.data / "logs", self.data / "reports", self.etc, self.units, self.out, self.bin):
            d.mkdir(parents=True)
        write_day(self.data / "logs" / "2026-11-03.jsonl", sample_day(), cut_short=True)
        (self.data / "reports" / "calibration.md").write_text("# calibration\n")
        (self.etc / "judge.env").write_text(f"TYPESAFE_API_KEY={KEY}\nDESK_JUDGE_SECRET={DESK_SECRET}\n")
        (self.etc / "desk.env").write_text(f"DESK_JUDGE_SECRET={DESK_SECRET}\n")
        (self.units / "ltppxt-desk.service").write_text("ExecStart=/opt/ltppxt/.venv/bin/python -m desk.run --mode shadow --bot house --feed coinbase\n")
        journal = self.bin / "journalctl"
        journal.write_text("#!/bin/sh\n"
                           "echo '2026-11-03T00:00:01.000000+00:00 vm python[1]: judge status=200 model=jev-1.13.0'\n"
                           f"echo '2026-11-03T00:00:02.000000+00:00 vm python[1]: Traceback (most recent call last): key {KEY}'\n")
        journal.chmod(0o755)

    def run(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", "LTPPXT_DATA": str(self.data),
               "LTPPXT_APP": str(self.root / "app"), "LTPPXT_ETC": str(self.etc), "LTPPXT_UNITS": str(self.units),
               "LTPPXT_EXPORT_DIR": str(self.out)}
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120)


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.vm = FakeVM(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_archive_holds_all_data_and_no_secret(self):
        r = self.vm.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        archives = list(self.vm.out.glob("ltppxt-export-*.tar.gz"))
        self.assertEqual(len(archives), 1)
        with tarfile.open(archives[0]) as tar:
            members = {m.name.split("/", 1)[1]: m for m in tar.getmembers() if "/" in m.name}
            for name in ("health.txt", "journal.txt", "system/etc-ltppxt-listing.txt", "system/units/ltppxt-desk.service",
                         "data/logs/2026-11-03.jsonl", "data/reports/calibration.md"):
                self.assertIn(name, members)
            contents = {n: tar.extractfile(m).read() for n, m in members.items() if m.isfile()}
        self.assertEqual(contents["data/logs/2026-11-03.jsonl"], (self.vm.data / "logs" / "2026-11-03.jsonl").read_bytes())
        for name, body in contents.items():
            self.assertNotIn(KEY.encode(), body, name)
            self.assertNotIn(DESK_SECRET.encode(), body, name)
        self.assertIn(b"[redacted]", contents["journal.txt"])
        self.assertNotIn(KEY, r.stdout)
        self.assertEqual(list(self.vm.out.glob(".*.part")), [])

    def test_stops_without_an_archive_when_the_data_holds_a_secret(self):
        with open(self.vm.data / "logs" / "2026-11-03.jsonl", "a") as f:
            f.write(json.dumps({"type": "error", "ts": DAY0, "detail": f"leaked {KEY}"}) + "\n")
        r = self.vm.run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("2026-11-03.jsonl", r.stderr)
        self.assertNotIn(KEY, r.stdout + r.stderr)
        self.assertEqual(list(self.vm.out.iterdir()), [])

    def test_health_only_writes_nothing(self):
        r = self.vm.run("--health-only")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LTPPXT health check", r.stdout)
        self.assertIn("desk runs: shadow mode, house bot", r.stdout)
        self.assertEqual(list(self.vm.out.iterdir()), [])

    def test_low_disk_stops_and_leaves_nothing_behind(self):
        export = load_export()
        export.DATA = self.vm.data
        export.MIN_FREE_BYTES = 1 << 62  # more than any disk has
        stage = self.vm.root / "stage"
        stage.mkdir()
        (stage / "health.txt").write_text("ok\n")
        final = self.vm.out / "x.tar.gz"
        with self.assertRaises(export.LowDisk):
            export.write_archive(final, "x", stage, [KEY])
        self.assertEqual(list(self.vm.out.iterdir()), [])


class HealthTest(unittest.TestCase):
    def test_counts_blocks_stalls_errors_and_simulated_trades(self):
        export = load_export()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-11-03.jsonl"
            write_day(path, sample_day(), cut_short=True)
            text = "\n".join(export.log_lines([path], now=DAY0 + 3600))
        row = next(ln for ln in text.splitlines() if ln.strip().startswith("2026-11-03"))
        # day, blocks, stalls, longest gap, starts, decisions, answers, errors, feed errors, hypothetical trades
        self.assertEqual(row.split(), ["2026-11-03", "330", "1", "5m10s", "1", "1", "1", "2", "1", "2"])
        self.assertIn("Unreadable log lines: 1", text)
        self.assertIn("jev_deadline", text)
        self.assertIn("SIMULATED", text)
        self.assertIn("round trips closed: 2 (holding_period: 1, stop_loss: 1); wins after costs: 1", text)
        self.assertIn("hypothetical net P&L: $0.25, mean +5.00 bps per trade", text)
        self.assertIn("Jev answers by model: jev-1.13.0: 1", text)

    def test_secrets_come_from_env_values_and_are_scrubbed(self):
        export = load_export()
        with tempfile.TemporaryDirectory() as tmp:
            etc = Path(tmp)
            (etc / "judge.env").write_text(f"TYPESAFE_API_KEY='{KEY}'\nSHORT=abc\n# a comment\n")
            secrets = export.secrets_in(etc)
        self.assertEqual(secrets, [KEY])
        self.assertEqual(export.scrub(f"x {KEY} y", secrets), "x [redacted] y")


if __name__ == "__main__":
    unittest.main()
