"""Question batteries, one module per bot type."""

from questions import gambler, house, spy
from questions.battery import OTHER_LABEL, Battery, Question

BOTS = {"gambler": gambler.battery, "house": house.battery, "spy": spy.battery}


def load_battery(bot: str, horizon_s: float) -> Battery:
    if bot not in BOTS:
        raise ValueError(f"unknown bot {bot!r}; known: {sorted(BOTS)}")
    return BOTS[bot](horizon_s)


__all__ = ["BOTS", "OTHER_LABEL", "Battery", "Question", "load_battery"]
