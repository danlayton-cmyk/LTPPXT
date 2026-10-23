"""Question battery types.

A battery is everything one bot asks Jev about one decision, sent in a single call.
Each question asks for one judgment. Every Choice gets an "other" option added here,
so a battery author cannot forget it.

Each question may carry a resolver: given the snapshot at decision time and the snapshot
one outcome horizon later, it returns the realized answer (bool for Noul, label for
Choice, level for Score), or None when the outcome cannot be measured from market data.
Resolved outcomes feed the calibration report.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from state.snapshot import Snapshot

OTHER_LABEL = "other"
OTHER_DESCRIPTION = "None of the listed options describes this situation."

Outcome = bool | str | int | None
Resolver = Callable[[Snapshot, Snapshot], Outcome]


@dataclass(frozen=True)
class Question:
    id: str
    kind: Literal["noul", "choice", "score"]
    instructions: str
    criteria: dict[str, str] | list[str] | None = None
    resolver: Resolver | None = None

    def to_wire(self) -> dict:
        """Raw TypeSafe question dict (the SDK accepts these as-is)."""
        wire: dict = {"type": self.kind, "instructions": self.instructions}
        if self.criteria is not None:
            wire["criteria"] = dict(self.criteria) if isinstance(self.criteria, dict) else list(self.criteria)
        return wire


def noul(id: str, instructions: str, resolver: Resolver | None = None) -> Question:
    return Question(id, "noul", instructions, None, resolver)


def choice(id: str, instructions: str, options: dict[str, str], resolver: Resolver | None = None) -> Question:
    if OTHER_LABEL in options:
        raise ValueError(f"{id}: do not pass '{OTHER_LABEL}', it is added automatically")
    return Question(id, "choice", instructions, {**options, OTHER_LABEL: OTHER_DESCRIPTION}, resolver)


def score(id: str, instructions: str, levels: list[str], resolver: Resolver | None = None) -> Question:
    if len(levels) < 2:
        raise ValueError(f"{id}: a score needs at least two levels")
    return Question(id, "score", instructions, list(levels), resolver)


@dataclass(frozen=True)
class Battery:
    bot: str
    version: str  # bump whenever wording changes, so calibration never mixes question versions
    questions: tuple[Question, ...]

    def __post_init__(self) -> None:
        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.bot}: duplicate question ids")
        for q in self.questions:
            if q.kind == "choice" and OTHER_LABEL not in (q.criteria or {}):
                raise ValueError(f"{self.bot}.{q.id}: choice without '{OTHER_LABEL}'")

    def to_wire(self) -> dict[str, dict]:
        return {q.id: q.to_wire() for q in self.questions}

    def by_id(self, qid: str) -> Question:
        return next(q for q in self.questions if q.id == qid)


# Shared resolver helpers. "move" is the mid change over the horizon in bps.


def move_bps(now: Snapshot, later: Snapshot) -> float:
    return (later.mid / now.mid - 1) * 1e4


# What counts as a real move over a horizon, in bps. Measured in spreads it was useless: BTC-USD
# quotes a 1-cent spread (~0.001 bps), so nearly every move was "more than two spreads" and the
# house-v1 questions resolved the same way about 90% of the time (2026-10-21..23 shadow logs).
# 10 bps at 5 minutes: in Coinbase 1-minute candles on 2026-10-23, 23-37% of 5-minute moves in
# BTC, ETH and SOL exceeded it. Other horizons scale by sqrt(time).
BAND_BPS_AT_5_MIN = 10.0


def band_bps(horizon_s: float) -> int:
    return round(BAND_BPS_AT_5_MIN * math.sqrt(horizon_s / 300))


def against_inventory(now: Snapshot, later: Snapshot) -> bool | None:
    if now.inventory == 0:
        return None
    m = move_bps(now, later)
    return m < 0 if now.inventory > 0 else m > 0


def horizon_text(horizon_s: float) -> str:
    minutes = horizon_s / 60
    return f"{minutes:g} minute" + ("" if minutes == 1 else "s")
