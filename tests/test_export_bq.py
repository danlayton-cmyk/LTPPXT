import json
import subprocess
import tempfile
import unittest
import unittest.mock
from datetime import UTC, datetime
from pathlib import Path

from cloud import export_bq
from logs.writer import EventLog

DAY_TS = 1_760_000_000.0  # 2025-10-09 UTC


class ExportBqTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = EventLog(self.dir, secrets=["sk-live-SECRET"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_lift_common_fields_and_keep_payload(self):
        self.log.write("decision", DAY_TS, decision_id="d1", bot="spy", model="jev-1.13.0", mode="shadow", action="hold", p=0.41)
        self.log.write("hypothetical_trade_closed", DAY_TS + 5, symbol="BTC-USD", net_pnl_usd=-1.25, gross_pnl_usd=0.5, side="buy")
        d = export_bq.read_day(self.dir, "2025-10-09")
        self.assertEqual(len(d.rows), 2)
        dec, trade = d.rows
        self.assertEqual(dec["day"], "2025-10-09")
        self.assertEqual(dec["ts"], "2025-10-09T08:53:20.000000Z")
        self.assertEqual((dec["type"], dec["bot"], dec["model"], dec["action"], dec["p"]), ("decision", "spy", "jev-1.13.0", "hold", 0.41))
        self.assertIsNone(dec["net_pnl_usd"])
        self.assertEqual(trade["net_pnl_usd"], -1.25)
        self.assertEqual(json.loads(trade["payload"])["side"], "buy")
        self.assertEqual((trade["source_file"], trade["line_no"]), ("2025-10-09.jsonl", 2))

    def test_secrets_stay_scrubbed(self):
        self.log.write("error", DAY_TS, detail="boom sk-live-SECRET", api_key="x")
        row = export_bq.read_day(self.dir, "2025-10-09").rows[0]
        self.assertNotIn("sk-live-SECRET", row["payload"])
        self.assertEqual(json.loads(row["payload"])["api_key"], "[redacted]")

    def test_partial_last_line_and_bad_json_are_skipped(self):
        self.log.write("equity", DAY_TS, equity_usd=1.0)
        with open(self.dir / "2025-10-09.jsonl", "a") as f:
            f.write("not json\n")
            f.write('{"ts": 1760000000, "type": "equ')  # being written, no newline yet
        d = export_bq.read_day(self.dir, "2025-10-09")
        self.assertEqual(len(d.rows), 1)
        self.assertEqual(d.skipped_partial, 2)

    def test_non_finite_and_bool_numbers_become_null(self):
        for v in (float("nan"), True, "0.5"):
            row = export_bq.to_row({"ts": DAY_TS, "type": "decision", "p": v}, "f", 1)
            self.assertIsNone(row["p"], v)

    def test_record_from_another_day_is_not_loaded_into_this_partition(self):
        with open(self.dir / "2025-10-09.jsonl", "w") as f:
            f.write(json.dumps({"ts": DAY_TS + 86400, "type": "equity"}) + "\n")
        d = export_bq.read_day(self.dir, "2025-10-09")
        self.assertEqual((len(d.rows), d.skipped_other_day), (0, 1))

    def test_default_days_are_yesterday_and_today_utc(self):
        now = datetime(2026, 11, 7, 0, 30, tzinfo=UTC)
        self.assertEqual(export_bq.days_to_export(self.dir, False, None, now), ["2026-11-06", "2026-11-07"])

    def test_load_command_replaces_one_partition(self):
        cmd = export_bq.bq_load_command("proj", "ltppxt", "2025-10-09", Path("/tmp/x.ndjson"), "US")
        self.assertIn("--replace", cmd)
        self.assertIn("proj:ltppxt.events$20251009", cmd)
        self.assertIn("--location=US", cmd)
        self.assertNotIn("--autodetect", cmd)

    def test_main_loads_each_day_and_never_replaces_with_nothing(self):
        self.log.write("equity", DAY_TS, equity_usd=1.0)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            rows = Path(cmd[-1]).read_text().splitlines()
            self.assertEqual(len(rows), 1)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with unittest.mock.patch("shutil.which", return_value="/usr/bin/bq"):
            rc = export_bq.main(["--logs", str(self.dir), "--project", "proj", "--all"], run=fake_run)
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            # a day with no file is skipped, not loaded empty
            calls.clear()
            rc = export_bq.main(["--logs", str(self.dir), "--project", "proj", "--day", "2025-10-10"], run=fake_run)
            self.assertEqual((rc, calls), (0, []))

    def test_main_reports_bq_failure(self):
        self.log.write("equity", DAY_TS, equity_usd=1.0)
        fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Access Denied")  # noqa: E731
        with unittest.mock.patch("shutil.which", return_value="/usr/bin/bq"):
            self.assertEqual(export_bq.main(["--logs", str(self.dir), "--project", "p", "--all"], run=fail), 1)

    def test_schema_matches_row_keys(self):
        cols = {c["name"] for c in json.loads(export_bq.SCHEMA_PATH.read_text())}
        row = export_bq.to_row({"ts": DAY_TS, "type": "equity"}, "f", 1)
        self.assertEqual(cols, set(row))


if __name__ == "__main__":
    unittest.main()
