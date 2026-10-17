"""Append-only JSONL event log, one file per UTC day.

Event types: snapshot, question, answer, decision, rejection, veto, order, fill,
outcome, equity, error, stop_loss, trade_closed, feed_error. Every record is scrubbed
before it is written: values under secret-looking keys are replaced, and any registered
secret value is removed from strings.
"""

import json
import re
import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

REDACTED = "[redacted]"
_SECRET_KEY = re.compile(r"(^|_)(api_?key|secret|token|authorization|password|bearer)$", re.IGNORECASE)


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


class EventLog:
    def __init__(self, root: str | Path, secrets: Iterable[str] = ()) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._secrets = [s for s in secrets if s]
        self._lock = threading.Lock()  # the live feed thread writes too

    def write(self, event_type: str, ts: float, **fields) -> dict:
        record = self._scrub({**fields, "ts": ts, "type": event_type})
        line = json.dumps(record, separators=(",", ":"), sort_keys=True, default=str)
        with self._lock, open(self.root / f"{utc_day(ts)}.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return record

    def _scrub(self, value, key: str = ""):
        if key and _SECRET_KEY.search(key):
            return REDACTED
        if isinstance(value, dict):
            return {k: self._scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub(v) for v in value]
        if isinstance(value, str):
            for s in self._secrets:
                value = value.replace(s, REDACTED)
        return value


def read_events(root: str | Path, day: str | None = None) -> Iterator[dict]:
    """Yield events from one day's file, or from every file in date order."""
    root = Path(root)
    files = [root / f"{day}.jsonl"] if day else sorted(root.glob("*.jsonl"))
    for path in files:
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
