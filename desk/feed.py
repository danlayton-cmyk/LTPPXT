"""Market data feeds.

A feed answers two things: the ticks published up to a time, and (as the funnel's single
"one-call" check) top-of-book depth. Two feeds ship here, both offline:

- SyntheticFeed: seeded random walk, for smoke tests.
- ReplayFeed: replays recorded ticks from a JSONL file.

A live public-data feed is not included. It needs no trading key, but it is a new network
dependency, so it is left for you to choose.
"""

import json
import math
import random
from bisect import bisect_right
from pathlib import Path
from typing import Protocol

from state.snapshot import Tick


class MarketFeed(Protocol):
    symbols: tuple[str, ...]

    def ticks(self, symbol: str, until_ts: float, lookback_s: float) -> list[Tick]:
        """Ticks with until_ts - lookback_s <= ts <= until_ts, oldest first."""

    def top_depth_usd(self, symbol: str, at_ts: float) -> float:
        """Smaller of bid and ask notional at the touch, from the latest tick at or before at_ts."""

    def first_ts(self) -> float:
        """Earliest tick time across symbols."""


class _TickStore:
    def __init__(self, by_symbol: dict[str, list[Tick]]) -> None:
        self._ticks = {s: sorted(t, key=lambda x: x.ts) for s, t in by_symbol.items()}
        self._ts = {s: [t.ts for t in ticks] for s, ticks in self._ticks.items()}
        self.symbols = tuple(sorted(self._ticks))

    def ticks(self, symbol: str, until_ts: float, lookback_s: float) -> list[Tick]:
        ts = self._ts.get(symbol, [])
        lo, hi = bisect_right(ts, until_ts - lookback_s - 1e-9), bisect_right(ts, until_ts)
        return self._ticks[symbol][lo:hi] if symbol in self._ticks else []

    def top_depth_usd(self, symbol: str, at_ts: float) -> float:
        i = bisect_right(self._ts.get(symbol, []), at_ts)
        if i == 0:
            return 0.0
        t = self._ticks[symbol][i - 1]
        return min(t.bid * t.bid_size, t.ask * t.ask_size)

    def first_ts(self) -> float:
        return min(ts[0] for ts in self._ts.values() if ts)


class SyntheticFeed(_TickStore):
    def __init__(self, symbols: tuple[str, ...], start_ts: float, seconds: int, seed: int = 7, tick_every_s: float = 1.0) -> None:
        rng = random.Random(seed)
        out: dict[str, list[Tick]] = {}
        for i, sym in enumerate(symbols):
            price = 100.0 * (i + 1)
            spread_bps = 2.0 + i
            ticks = []
            n = int(seconds / tick_every_s)
            for k in range(n):
                price *= math.exp(rng.gauss(0, 2e-4))
                half = price * spread_bps / 2e4
                ticks.append(Tick(start_ts + k * tick_every_s, sym, price - half, price + half, rng.uniform(5, 50), rng.uniform(5, 50)))
            out[sym] = ticks
        super().__init__(out)


class ReplayFeed(_TickStore):
    """JSONL, one tick per line: {"ts":..., "symbol":..., "bid":..., "ask":..., "bid_size":..., "ask_size":...}"""

    def __init__(self, path: str | Path) -> None:
        out: dict[str, list[Tick]] = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    t = Tick(**json.loads(line))
                    out.setdefault(t.symbol, []).append(t)
        super().__init__(out)
