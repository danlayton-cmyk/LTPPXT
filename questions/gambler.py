"""Gambler: a momentum taker. Crosses the spread when a move looks likely to continue."""

from questions.battery import Battery, against_inventory, choice, horizon_text, move_bps, noul, score


def battery(horizon_s: float) -> Battery:
    h = horizon_text(horizon_s)

    def direction(now, later):
        m, band = move_bps(now, later), now.spread_bps
        return "up" if m > band else "down" if m < -band else "flat"

    return Battery(
        bot="gambler",
        version="gambler-v1",
        questions=(
            choice(
                "regime",
                "Using `return_bps` over the 1m, 5m and 15m windows and `realized_vol_bps`, "
                "which regime best describes how this market is trading right now?",
                {
                    "trending_up": "Price is rising steadily across the windows, with pullbacks small relative to the move.",
                    "trending_down": "Price is falling steadily across the windows, with bounces small relative to the move.",
                    "ranging": "Price is oscillating around a level with no sustained direction.",
                    "disorderly": "Moves are large relative to the trend and direction flips quickly.",
                },
            ),
            choice(
                "direction",
                f"Where will `mid` be {h} from now relative to its current value?",
                {
                    "up": "Higher by more than the current spread (`spread_bps`).",
                    "down": "Lower by more than the current spread.",
                    "flat": "Within one spread of the current mid.",
                },
                resolver=direction,
            ),
            score(
                "chase_risk",
                "How exposed is a position opened at the current `mid` to an immediate reversal of the recent move in `return_bps`?",
                [
                    "Low: the move is recent and modest relative to `realized_vol_bps`, with room to continue.",
                    "Moderate: the move is meaningful but not stretched.",
                    "High: the move is large relative to `realized_vol_bps` and looks stretched.",
                    "Extreme: the move looks exhausted and a snap-back is the likelier next step.",
                ],
            ),
            noul(
                "exit_now",
                f"The desk holds `inventory` (see `position`). Will `mid` move against this position over the next {h}?",
                resolver=against_inventory,
            ),
        ),
    )
