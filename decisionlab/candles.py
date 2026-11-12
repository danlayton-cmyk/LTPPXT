"""Public Coinbase Exchange 1-minute candles, cached to disk. No key, no account.

    python -m decisionlab.candles --out data/candles --days 90 --end 2026-11-11T00:00:00Z

Writes one CSV per symbol (ts,low,high,open,close,volume; ts is the candle's start, epoch
seconds, ascending) and manifest.json with the exact range, row counts and SHA-256 of each
file, so a study can name the data it ran on. Market data only: nothing here can trade.
"""

import argparse
import csv
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BASE_URL = "https://api.exchange.coinbase.com"
MAX_PER_REQUEST = 300  # Coinbase's cap on candles per call
MIN_INTERVAL_S = 0.25  # 4 requests/s, under the public limit of 10/s
SYMBOLS = ("BTC-USD", "ETH-USD", "SOL-USD")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_range(symbol: str, start: int, end: int, opener=urllib.request.urlopen, sleep=time.sleep) -> list[list[float]]:
    """All 1-minute candles with start <= ts < end, ascending, de-duplicated."""
    rows: dict[int, list[float]] = {}
    step = MAX_PER_REQUEST * 60
    for lo in range(start, end, step):
        hi = min(end, lo + step)
        url = f"{BASE_URL}/products/{symbol}/candles?granularity=60&start={_iso(lo)}&end={_iso(hi - 60)}"
        req = urllib.request.Request(url, headers={"User-Agent": "ltppxt-research", "Accept": "application/json"})
        for attempt in range(5):
            try:
                with opener(req, timeout=20) as resp:
                    batch = json.loads(resp.read())
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 4:
                    raise
                sleep(2.0 * (attempt + 1))
        for c in batch:
            if lo <= int(c[0]) < hi:
                rows[int(c[0])] = [float(x) for x in c[1:6]]
        sleep(MIN_INTERVAL_S)
    return [[ts, *rows[ts]] for ts in sorted(rows)]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> list[list[float]]:
    with open(path, newline="") as f:
        r = csv.reader(f)
        next(r)
        return [[float(x) for x in row] for row in r]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--end", required=True, help="UTC, e.g. 2026-11-11T00:00:00Z. Fixed, so a rerun fetches the same range.")
    ap.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    args = ap.parse_args(argv)
    end = int(datetime.fromisoformat(args.end.replace("Z", "+00:00")).timestamp()) // 60 * 60
    start = end - args.days * 86400
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"source": f"{BASE_URL}/products/{{symbol}}/candles?granularity=60", "start": _iso(start), "end": _iso(end), "files": {}}
    for symbol in args.symbols:
        rows = fetch_range(symbol, start, end)
        path = out / f"{symbol}.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "low", "high", "open", "close", "volume"])
            w.writerows([[int(r[0]), *r[1:]] for r in rows])
        manifest["files"][symbol] = {"rows": len(rows), "expected_minutes": (end - start) // 60, "sha256": sha256(path)}
        print(f"{symbol}: {len(rows)} candles of {(end - start) // 60} minutes")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
