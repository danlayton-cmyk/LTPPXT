"""Gambler: a momentum taker. Crosses the spread when a move looks likely to continue."""

from questions.battery import Battery, against_inventory, band_bps, choice, horizon_text, move_bps, noul, score


def battery(horizon_s: float) -> Battery:
    h, b = horizon_text(horizon_s), band_bps(horizon_s)

    def direction(now, later):
        m = move_bps(now, later)
        return "up" if m > b else "down" if m < -b else "flat"

    return Battery(
        bot="gambler",
        version="gambler-v2",
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
                    "up": f"Higher by more than {b} bps.",
                    "down": f"Lower by more than {b} bps.",
                    "flat": f"Within {b} bps of the current mid.",
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
