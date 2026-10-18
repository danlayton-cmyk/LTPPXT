"""Deterministic market snapshot.

build_snapshot() turns raw ticks into the compact state Jev sees. It only reads ticks
stamped strictly before the decision time, and the account fields come from the book as
it stood before the decision. Same inputs, same output: no clocks, no randomness, no I/O.
"""

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from desk.config import StateConfig


@dataclass(frozen=True)
class Tick:
    ts: float  # venue publish time, epoch seconds
    symbol: str
    bid: float
    ask: float
    bid_size: float  # base units at the best bid
    ask_size: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class AccountView:
    """Book state as known before the decision."""

    inventory: float  # signed base units held in this symbol
    equity_usd: float
    peak_equity_usd: float


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    as_of: float  # decision time; every input is stamped strictly before it
    data_ts: float  # timestamp of the newest tick used
    mid: float
    spread_bps: float
    imbalance: float  # (bid_size - ask_size) / (bid_size + ask_size), in [-1, 1]
    rvol_bps: float  # realized vol over the vol window: sqrt(sum of squared log returns)
    returns_bps: dict[str, float | None]  # keyed by window label, None if history is too short
    inventory: float
    drawdown_pct: float
    has_book: bool = True  # False when the newest tick has no real book (e.g. a backfilled candle close)

    def to_state(self) -> dict:
        """The compact dict sent to Jev. Keys are self-describing so questions can cite them."""
        return {
            "symbol": self.symbol,
            "mid": _sig(self.mid),
            "spread_bps": round(self.spread_bps, 2),
            "book_imbalance": round(self.imbalance, 3),
            "realized_vol_bps": round(self.rvol_bps, 2),
            "return_bps": {k: None if v is None else round(v, 2) for k, v in self.returns_bps.items()},
            "inventory": _sig(self.inventory),
            "position": "long" if self.inventory > 0 else "short" if self.inventory < 0 else "flat",
            "drawdown_pct": round(self.drawdown_pct, 2),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_state(), separators=(",", ":"), sort_keys=True)


def approx_tokens(text: str) -> int:
    """Conservative token estimate. Jev's tokenizer is not public, so this assumes
    3 characters per token, which overcounts for typical English and JSON."""
    return math.ceil(len(text) / 3)


def window_label(seconds: int) -> str:
    return f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"


def build_snapshot(ticks: Sequence[Tick], decision_ts: float, account: AccountView, cfg: StateConfig) -> Snapshot | None:
    """Build the snapshot for one symbol at decision_ts, or None when no usable tick exists."""
    past = sorted((t for t in ticks if t.ts < decision_ts), key=lambda t: t.ts)
    if not past:
        return None
    symbols = {t.symbol for t in past}
    if len(symbols) != 1:
        raise ValueError(f"build_snapshot takes one symbol, got {sorted(symbols)}")
    last = past[-1]
    mid = last.mid
    if mid <= 0 or last.ask < last.bid:
        return None

    depth = last.bid_size + last.ask_size
    imbalance = (last.bid_size - last.ask_size) / depth if depth > 0 else 0.0

    returns: dict[str, float | None] = {}
    for w in cfg.return_windows_s:
        ref = _mid_at_or_before(past, decision_ts - w)
        returns[window_label(w)] = None if ref is None else math.log(mid / ref) * 1e4

    vol_ticks = [t.mid for t in past if t.ts >= decision_ts - cfg.vol_window_s]
    log_rets = [math.log(b / a) for a, b in zip(vol_ticks, vol_ticks[1:]) if a > 0 and b > 0]
    rvol = math.sqrt(sum(r * r for r in log_rets)) * 1e4

    peak = max(account.peak_equity_usd, account.equity_usd)
    drawdown = (peak - account.equity_usd) / peak * 100 if peak > 0 else 0.0

    snap = Snapshot(
        symbol=last.symbol,
        as_of=decision_ts,
        data_ts=last.ts,
        mid=mid,
        spread_bps=(last.ask - last.bid) / mid * 1e4,
        imbalance=imbalance,
        rvol_bps=rvol,
        returns_bps=returns,
        inventory=account.inventory,
        drawdown_pct=drawdown,
        has_book=last.bid_size > 0 and last.ask_size > 0 and last.ask > last.bid,
    )
    tokens = approx_tokens(snap.to_json())
    if tokens >= cfg.max_state_tokens:
        raise ValueError(f"snapshot is ~{tokens} tokens, budget is {cfg.max_state_tokens}")
    return snap


def _mid_at_or_before(past: Sequence[Tick], ts: float) -> float | None:
    ref = None
    for t in past:
        if t.ts > ts:
            break
        ref = t.mid
    return ref


def _sig(x: float, digits: int = 6) -> float:
    return float(f"{x:.{digits}g}")
