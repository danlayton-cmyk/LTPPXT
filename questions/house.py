"""House: a passive liquidity provider. Rests one order at the touch and avoids informed flow."""

from questions.battery import Battery, against_inventory, choice, horizon_text, move_bps, noul, score


def battery(horizon_s: float) -> Battery:
    h = horizon_text(horizon_s)

    def toxic(now, later):
        return abs(move_bps(now, later)) > 2 * now.spread_bps

    def lean(now, later):
        m, band = move_bps(now, later), 2 * now.spread_bps
        if m == 0 or abs(m) > band:
            return "neither"
        return "bid" if m > 0 else "ask"

    return Battery(
        bot="house",
        version="house-v1",
        questions=(
            noul(
                "toxic_flow",
                f"Given `book_imbalance` and the short-window `return_bps`, will `mid` move more than two spreads "
                f"(`spread_bps`) in either direction over the next {h}, so that a passive order filled now would be quickly underwater?",
                resolver=toxic,
            ),
            choice(
                "quote_lean",
                f"For a single passive order resting at the touch for the next {h}, which side is safest to rest on?",
                {
                    "bid": "Rest a buy at the best bid: price is likely to drift up gently, within two spreads.",
                    "ask": "Rest a sell at the best ask: price is likely to drift down gently, within two spreads.",
                    "neither": "Rest nothing: price is likely to move more than two spreads, or not at all.",
                },
                resolver=lean,
            ),
            score(
                "liquidity_calm",
                "How calm and two-sided is liquidity in this market right now, judging from `spread_bps`, `book_imbalance` and `realized_vol_bps`?",
                [
                    "Stressed: wide or unstable spread, one-sided book, large recent moves.",
                    "Uneasy: spread or imbalance elevated, moves above normal.",
                    "Normal: ordinary spread and balance for this market.",
                    "Calm: tight spread, balanced book, small recent moves.",
                ],
            ),
            noul(
                "inventory_adverse",
                f"The desk holds `inventory` (see `position`). Will `mid` move against this position over the next {h}?",
                resolver=against_inventory,
            ),
        ),
    )
