"""Code-only stop-loss.

Checked every block before the judge is asked, so it fires when the judge is slow,
rate-limited or down. Its only inputs are the entry price, the latest mid and a config
number; nothing a model returns can move or cancel it.
"""

from broker.base import Side


def adverse_move_bps(side: Side, entry_price: float, mid: float) -> float:
    """How far the mid has moved against a position, in bps of entry. Negative means in profit."""
    move = (mid - entry_price) / entry_price * 1e4
    return -move if side == "buy" else move


def stop_hit(side: Side, entry_price: float, mid: float, stop_loss_bps: float) -> float | None:
    """The adverse move in bps if it has reached the stop, else None."""
    adverse = adverse_move_bps(side, entry_price, mid)
    return adverse if adverse >= stop_loss_bps else None
