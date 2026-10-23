"""Spy: reads the order book for signs of an informed participant and follows them."""

from questions.battery import Battery, against_inventory, band_bps, choice, horizon_text, move_bps, noul


def battery(horizon_s: float) -> Battery:
    h, b = horizon_text(horizon_s), band_bps(horizon_s)

    def informed(now, later):
        m = move_bps(now, later)
        return "buyers" if m > b else "sellers" if m < -b else "none"

    def persists(now, later):
        if now.imbalance == 0:
            return None
        return (later.imbalance > 0) == (now.imbalance > 0) and abs(later.imbalance) >= 0.2

    def follows(now, later):
        if now.imbalance == 0:
            return None
        side = 1 if now.imbalance > 0 else -1
        return move_bps(now, later) * side >= b

    return Battery(
        bot="spy",
        version="spy-v2",
        questions=(
            choice(
                "informed_side",
                "Judging from `book_imbalance` together with the short-window `return_bps`, which side, if any, "
                "shows signs of an informed participant working an order?",
                {
                    "buyers": f"Someone appears to be accumulating: bid size dominates and price will rise more than {b} bps over the next {h}.",
                    "sellers": f"Someone appears to be distributing: ask size dominates and price will fall more than {b} bps over the next {h}.",
                    "none": f"No sign of one-sided informed flow: price stays within {b} bps.",
                },
                resolver=informed,
            ),
            noul(
                "imbalance_persists",
                f"Will the sign of `book_imbalance` still be the same, with magnitude at least 0.2, {h} from now, "
                "rather than the resting size being pulled?",
                resolver=persists,
            ),
            noul(
                "follow_through",
                f"Will `mid` move at least {b} bps in the direction `book_imbalance` points within the next {h}?",
                resolver=follows,
            ),
            noul(
                "exit_now",
                f"The desk holds `inventory` (see `position`). Will `mid` move against this position over the next {h}?",
                resolver=against_inventory,
            ),
        ),
    )
