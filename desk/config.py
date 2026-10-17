"""Desk configuration.

Every number here is a starting default, not a tuned value. Change them in one place.
Secrets never live here: the desk reads DESK_JUDGE_SECRET from the environment and
never sees the TypeSafe key at all (see judge/server.py).
"""

from dataclasses import dataclass, field
from enum import Enum

# Pinned Jev version. jev-latest and jev-preview are aliases that move when TypeSafe
# ships a release, so they are refused everywhere. Source: docs.typesafe.ai/models
# (checked 2026-10-17: jev-latest -> jev-1.13.0, jev-preview -> jev-1.14.0).
PINNED_JEV_MODEL = "jev-1.13.0"
FORBIDDEN_MODEL_ALIASES = frozenset({"jev-latest", "jev-preview"})

# Model string the offline fake judge reports. Risk vetoes it, reports exclude it.
OFFLINE_FAKE_MODEL = "offline-fake"


class Mode(str, Enum):
    SHADOW = "shadow"  # Jev labels every decision, policy logs what it would do, nothing executes
    PAPER = "paper"  # policy executes against the paper broker
    # There is deliberately no LIVE mode.


@dataclass(frozen=True)
class StateConfig:
    return_windows_s: tuple[int, int, int] = (60, 300, 900)
    vol_window_s: int = 900
    max_state_tokens: int = 400


@dataclass(frozen=True)
class FeeConfig:
    taker_fee_bps: float = 5.0
    maker_fee_bps: float = 1.0
    gas_usd_per_fill: float = 0.0  # set above zero for on-chain venues
    slippage_bps: float = 1.0  # paper market orders fill this far through the touch


@dataclass(frozen=True)
class RiskConfig:
    max_position_usd: float = 1_000.0
    daily_loss_limit_usd: float = 50.0
    max_leverage: float = 1.0
    range_guard_bps: float = 50.0  # order price must sit within this distance of the last mid
    max_mid_jump_bps: float = 300.0  # mid vs the previous block's mid; larger means bad data
    max_data_age_s: float = 5.0
    fee_edge_margin: float = 1.5  # expected edge must cover fees + gas + spread this many times
    stop_loss_bps: float = 75.0  # adverse move from entry that forces a market exit, no model asked


@dataclass(frozen=True)
class FunnelConfig:
    max_spread_bps: float = 20.0
    max_tick_age_s: float = 5.0
    min_vol_bps: float = 1.0
    max_vol_bps: float = 400.0
    min_top_depth_usd: float = 2_000.0
    max_jev_candidates: int = 3


# Seconds a symbol stays benched, keyed by the check that rejected it. Cheap, transient
# reasons clear fast; reasons that say something about the market or the model last longer.
DEFAULT_BENCH_SECONDS: dict[str, float] = {
    "no_data": 30,
    "stale_data": 30,
    "insufficient_history": 60,
    "spread_too_wide": 60,
    "vol_out_of_range": 300,
    "thin_book": 120,
    "costs_exceed_move": 300,
    "order_exceeds_max_position": 3_600,
    "jev_deadline": 30,
    "jev_rate_limited": 60,
    "jev_unavailable": 60,
    "jev_malformed": 86_400,  # a bad battery will not fix itself; needs a human
    "below_threshold": 120,
    "risk_veto": 900,
    "stop_loss": 1_800,
}


@dataclass(frozen=True)
class DeskConfig:
    mode: Mode = Mode.SHADOW
    bot: str = "gambler"
    symbols: tuple[str, ...] = ("BTC-USD", "ETH-USD", "SOL-USD")
    starting_cash_usd: float = 10_000.0
    order_size_usd: float = 250.0
    allow_short: bool = True
    block_interval_s: float = 10.0
    outcome_horizon_s: float = 300.0
    judge_url: str = "http://127.0.0.1:8787"
    jev_model: str = PINNED_JEV_MODEL
    log_dir: str = "logs/data"
    state: StateConfig = field(default_factory=StateConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    funnel: FunnelConfig = field(default_factory=FunnelConfig)
    bench_seconds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BENCH_SECONDS))

    def __post_init__(self) -> None:
        if not isinstance(self.mode, Mode):
            raise ValueError(f"unknown mode {self.mode!r}; only shadow and paper exist")
        check_pinned_model(self.jev_model)


def check_pinned_model(model: str) -> None:
    """Refuse aliases. A pinned model is a full versioned ID such as jev-1.13.0."""
    parts = model.removeprefix("jev-").split(".")
    if model in FORBIDDEN_MODEL_ALIASES or not model.startswith("jev-") or len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"model must be a pinned versioned ID like jev-1.13.0, got {model!r}")
