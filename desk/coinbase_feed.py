"""Live market data from Coinbase Exchange's public level-1 order book. No API key.

    GET https://api.exchange.coinbase.com/products/{symbol}/book?level=1

A background thread polls each symbol about once per `poll_interval_s` and keeps a rolling
window of ticks in memory. Each tick is stamped with the exchange's own `time` field,
so data age and the before-the-decision rule use exchange time, not arrival time.

- Coinbase caches this response for up to 2 s (cache-control: max-age=2), so a poll can
  return the same book twice; repeats are dropped by `sequence`.
- Coinbase's public limit is 10 requests/s per IP (burst 15). This feed refuses
  configurations above MAX_REQUESTS_PER_S and backs off on 429.
- Market data only. Nothing here can place an order.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from datetime import UTC, datetime

from state.snapshot import Tick

BASE_URL = "https://api.exchange.coinbase.com"
MAX_REQUESTS_PER_S = 5.0  # half of Coinbase's public limit
USER_AGENT = "ltppxt-paper-desk"


class RateLimited(Exception):
    pass


def fetch_level1(symbol: str, timeout_s: float = 3.0) -> dict:
    req = urllib.request.Request(f"{BASE_URL}/products/{symbol}/book?level=1", headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(symbol) from None
        raise


def parse_time(s: str) -> float:
    """RFC 3339 with up to nanosecond precision, e.g. 2026-10-17T22:48:17.780493982Z."""
    base, _, frac = s.rstrip("Z").partition(".")
    ts = datetime.fromisoformat(base).replace(tzinfo=UTC).timestamp()
    return ts + (float("0." + frac) if frac else 0.0)


def parse_book(symbol: str, book: dict) -> Tick | None:
    if not book.get("bids") or not book.get("asks") or not book.get("time"):
        return None
    bid, bid_size = float(book["bids"][0][0]), float(book["bids"][0][1])
    ask, ask_size = float(book["asks"][0][0]), float(book["asks"][0][1])
    if bid <= 0 or ask < bid:
        return None
    return Tick(parse_time(book["time"]), symbol, bid, ask, bid_size, ask_size)


class CoinbaseFeed:
    def __init__(
        self,
        symbols: tuple[str, ...],
        history_s: float,
        poll_interval_s: float = 1.0,
        fetch: Callable[[str], dict] = fetch_level1,
        on_error: Callable[[str, str], None] | None = None,
    ) -> None:
        if len(symbols) / poll_interval_s > MAX_REQUESTS_PER_S:
            raise ValueError(f"{len(symbols)} symbols every {poll_interval_s}s exceeds {MAX_REQUESTS_PER_S} requests/s")
        self.symbols = tuple(symbols)
        self._history_s = history_s
        self._interval = poll_interval_s
        self._fetch = fetch
        self._on_error = on_error or (lambda symbol, detail: None)
        self._ticks: dict[str, list[Tick]] = {s: [] for s in self.symbols}
        self._last_seq: dict[str, int] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.backoff_s = 0.0

    # -- MarketFeed ------------------------------------------------------------------

    def ticks(self, symbol: str, until_ts: float, lookback_s: float) -> list[Tick]:
        with self._lock:
            ticks = self._ticks.get(symbol, [])
            ts = [t.ts for t in ticks]
            lo, hi = bisect_right(ts, until_ts - lookback_s - 1e-9), bisect_right(ts, until_ts)
            return ticks[lo:hi]

    def top_depth_usd(self, symbol: str, at_ts: float) -> float:
        recent = self.ticks(symbol, at_ts, self._history_s)
        if not recent:
            return 0.0
        t = recent[-1]
        return min(t.bid * t.bid_size, t.ask * t.ask_size)

    def first_ts(self) -> float:
        with self._lock:
            firsts = [t[0].ts for t in self._ticks.values() if t]
        return min(firsts) if firsts else time.time()

    # -- polling ---------------------------------------------------------------------

    def poll_once(self) -> None:
        """One pass over all symbols. Raises nothing; errors go to on_error."""
        for symbol in self.symbols:
            try:
                book = self._fetch(symbol)
            except RateLimited:
                self.backoff_s = min(30.0, max(1.0, self.backoff_s * 2))
                self._on_error(symbol, f"rate limited, backing off {self.backoff_s:.0f}s")
                return
            except Exception as e:  # network errors must not kill a 24/7 feed
                self._on_error(symbol, f"{type(e).__name__}: {e}")
                continue
            self.backoff_s = 0.0
            seq = book.get("sequence")
            if seq is not None and seq == self._last_seq.get(symbol):
                continue  # cached repeat
            try:
                tick = parse_book(symbol, book)
            except (ValueError, TypeError, IndexError, KeyError) as e:
                self._on_error(symbol, f"unparseable book: {e}")
                continue
            if tick is None:
                continue
            self._last_seq[symbol] = seq
            with self._lock:
                buf = self._ticks[symbol]
                if buf and tick.ts <= buf[-1].ts:
                    continue  # never let time run backwards
                buf.append(tick)
                cutoff = tick.ts - self._history_s
                drop = bisect_left([t.ts for t in buf], cutoff)  # keep a tick exactly history_s old
                if drop:
                    del buf[:drop]

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                started = time.monotonic()
                self.poll_once()
                wait = self.backoff_s or max(0.0, self._interval - (time.monotonic() - started))
                self._stop.wait(wait)

        self._thread = threading.Thread(target=loop, name="coinbase-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
