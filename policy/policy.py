"""Turn raw Jev answers into one action.

Everything that decides lives here in code, not in the model: which answers feed which
action, with what weight, and how sure the desk must be before acting.

Thresholds: one per action, from the break-even probability of a bet that wins G bps if
right and loses L bps if wrong: p* = L / (L + G). With an expected move of M bps and a
cost of C bps, an entry wins M - C and loses M + C, so p* = (M + C) / 2M. Costs are
inflated by the risk fee margin first, so the policy never picks a trade the fees-plus-gas
veto would block. An exit pays only one leg of costs, so its bar is lower. Each action
also has a floor. When costs reach the expected move, p* hits 1 and the action is impossible.

The combined probability is a weighted average of Jev probabilities. That is a heuristic,
not a calibrated probability: check it against outcomes before trusting the thresholds.
"""

import math
from dataclasses import dataclass, field
from enum import Enum

from desk.config import DeskConfig, FeeConfig
from state.snapshot import Snapshot


class Action(str, Enum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"
    HOLD = "hold"


ACTION_FLOORS = {Action.ENTER_LONG: 0.60, Action.ENTER_SHORT: 0.60, Action.EXIT: 0.50}

# weights[bot][action][feature]; features come from extract_features() and are all in [0, 1].
WEIGHTS: dict[str, dict[Action, dict[str, float]]] = {
    "gambler": {
        Action.ENTER_LONG: {"dir_up": 0.6, "regime_up": 0.2, "no_chase": 0.2},
        Action.ENTER_SHORT: {"dir_down": 0.6, "regime_down": 0.2, "no_chase": 0.2},
        Action.EXIT: {"against": 1.0},
    },
    "house": {
        Action.ENTER_LONG: {"lean_bid": 0.6, "not_toxic": 0.25, "calm": 0.15},
        Action.ENTER_SHORT: {"lean_ask": 0.6, "not_toxic": 0.25, "calm": 0.15},
        Action.EXIT: {"against": 1.0},
    },
    "spy": {
        Action.ENTER_LONG: {"buyers": 0.5, "follow_long": 0.3, "persist_long": 0.2},
        Action.ENTER_SHORT: {"sellers": 0.5, "follow_short": 0.3, "persist_short": 0.2},
        Action.EXIT: {"against": 1.0},
    },
}

# house rests passive limit orders and pays maker fees; the others cross the spread.
PASSIVE_BOTS = frozenset({"house"})


@dataclass(frozen=True)
class ActionScore:
    action: Action
    p: float
    threshold: float
    contributions: dict[str, float]

    @property
    def margin(self) -> float:
        return self.p - self.threshold


@dataclass(frozen=True)
class Decision:
    bot: str
    symbol: str
    action: Action
    p: float
    threshold: float
    expected_move_bps: float
    cost_bps: float
    passive: bool
    scores: dict[str, ActionScore] = field(default_factory=dict)

    @property
    def gross_edge_bps(self) -> float:
        """(2p - 1) * M: expected value of the bet in bps, before costs."""
        return (2 * self.p - 1) * self.expected_move_bps

    def to_log(self) -> dict:
        return {
            "bot": self.bot,
            "symbol": self.symbol,
            "action": self.action.value,
            "p": round(self.p, 4),
            "threshold": round(self.threshold, 4),
            "expected_move_bps": round(self.expected_move_bps, 3),
            "cost_bps": round(self.cost_bps, 3),
            "gross_edge_bps": round(self.gross_edge_bps, 3),
            "scores": {k: {"p": round(s.p, 4), "threshold": round(s.threshold, 4), "contributions": s.contributions} for k, s in self.scores.items()},
        }


def expected_move_bps(snap: Snapshot, cfg: DeskConfig) -> float:
    """One-sigma move over the outcome horizon, scaled from realized vol over the vol window."""
    return snap.rvol_bps * math.sqrt(cfg.outcome_horizon_s / cfg.state.vol_window_s)


def cost_bps(snap: Snapshot, fees: FeeConfig, notional_usd: float, passive: bool, legs: int) -> float:
    """Fees + gas + spread crossing for `legs` fills, in bps of notional."""
    fee = fees.maker_fee_bps if passive else fees.taker_fee_bps
    half_spread = 0.0 if passive else snap.spread_bps / 2 + fees.slippage_bps
    gas = fees.gas_usd_per_fill / notional_usd * 1e4 if notional_usd > 0 else math.inf
    return legs * (fee + half_spread + gas)


def threshold(action: Action, move_bps: float, cost: float) -> float:
    if move_bps <= 0:
        return 1.0
    return min(1.0, max(ACTION_FLOORS[action], (move_bps + cost) / (2 * move_bps)))


def extract_features(bot: str, answers: dict, snap: Snapshot) -> dict[str, float]:
    """Pull the probabilities each bot's weights refer to out of raw Jev answers."""

    def cp(qid: str, label: str) -> float:
        return float(answers[qid]["probabilities"].get(label, 0.0))

    def np_(qid: str) -> float:
        return float(answers[qid]["noul"])

    def norm_score(qid: str) -> float:
        a = answers[qid]
        top = max(int(k) for k in a["probabilities"])
        return float(a["score"]) / top if top else 0.0

    if bot == "gambler":
        return {
            "dir_up": cp("direction", "up"),
            "dir_down": cp("direction", "down"),
            "regime_up": cp("regime", "trending_up"),
            "regime_down": cp("regime", "trending_down"),
            "no_chase": 1 - norm_score("chase_risk"),
            "against": np_("exit_now"),
        }
    if bot == "house":
        return {
            "lean_bid": cp("quote_lean", "bid"),
            "lean_ask": cp("quote_lean", "ask"),
            "not_toxic": 1 - np_("toxic_flow"),
            "calm": norm_score("liquidity_calm"),
            "against": np_("inventory_adverse"),
        }
    if bot == "spy":
        up, down = snap.imbalance > 0, snap.imbalance < 0
        follow, persist = np_("follow_through"), np_("imbalance_persists")
        return {
            "buyers": cp("informed_side", "buyers"),
            "sellers": cp("informed_side", "sellers"),
            "follow_long": follow if up else 0.0,
            "follow_short": follow if down else 0.0,
            "persist_long": persist if up else 0.0,
            "persist_short": persist if down else 0.0,
            "against": np_("exit_now"),
        }
    raise ValueError(f"no feature map for bot {bot!r}")


def decide(bot: str, answers: dict, snap: Snapshot, cfg: DeskConfig, has_position: bool) -> Decision:
    """Pick the action whose probability clears its threshold by the widest margin, else hold.

    With a position open only exit is considered; when flat only entries are.
    """
    passive = bot in PASSIVE_BOTS
    move = expected_move_bps(snap, cfg)
    feats = extract_features(bot, answers, snap)
    if has_position:
        candidates = [Action.EXIT]
    else:
        candidates = [Action.ENTER_LONG] + ([Action.ENTER_SHORT] if cfg.allow_short else [])

    scores: dict[str, ActionScore] = {}
    for action in candidates:
        weights = WEIGHTS[bot][action]
        contributions = {f: round(w * feats[f], 4) for f, w in weights.items()}
        p = sum(w * feats[f] for f, w in weights.items()) / sum(weights.values())
        legs = 1 if action == Action.EXIT else 2
        c = cost_bps(snap, cfg.fees, cfg.order_size_usd, passive and action != Action.EXIT, legs)  # exits cross the spread
        scores[action.value] = ActionScore(action, p, threshold(action, move, c * cfg.risk.fee_edge_margin), contributions)

    best = max(scores.values(), key=lambda s: s.margin)
    is_exit = best.action == Action.EXIT
    chosen = best.action if best.margin >= 0 else Action.HOLD
    return Decision(
        bot=bot,
        symbol=snap.symbol,
        action=chosen,
        p=best.p,
        threshold=best.threshold,
        expected_move_bps=move,
        cost_bps=cost_bps(snap, cfg.fees, cfg.order_size_usd, passive and not is_exit, 1 if is_exit else 2),
        passive=passive and not is_exit,
        scores=scores,
    )
