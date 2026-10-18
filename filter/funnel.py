"""The funnel: cheapest check first.

1. free_cut    - data already in hand: bench, missing/no-book/stale data, history, spread, vol
2. one_call    - one data call per survivor: top-of-book depth
3. hard_facts  - facts in code: costs vs expected move, order size vs position limit
4. jev         - one judge call per survivor (capped), whole battery per call
5. final_pick  - policy thresholds, then the single best margin

Every rejection is logged with the stage and check that fired, and benches the symbol
for as long as that check calls for.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from book.book import Bench
from desk.config import DeskConfig
from desk.feed import MarketFeed
from judge.client import JudgeError, JudgeRateLimited, JudgeResult
from logs.writer import EventLog
from policy.policy import Action, Decision, cost_bps, decide, expected_move_bps
from questions.battery import Battery
from state.snapshot import Snapshot

# Checks that are about our capacity, not the symbol: logged, never benched.
UNBENCHED_CHECKS = frozenset({"jev_capacity", "jev_cooldown"})


@dataclass(frozen=True)
class Rejection:
    symbol: str
    stage: str
    check: str
    detail: str


@dataclass(frozen=True)
class Judged:
    decision_id: str
    snapshot: Snapshot
    result: JudgeResult
    decision: Decision


@dataclass
class FunnelResult:
    pick: Judged | None = None
    judged: list[Judged] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    rate_limited_for_s: float | None = None


class Funnel:
    def __init__(self, cfg: DeskConfig, battery: Battery, feed: MarketFeed, judge, bench: Bench, log: EventLog) -> None:
        self.cfg, self.battery, self.feed, self.judge, self.bench, self.log = cfg, battery, feed, judge, bench, log

    def run(self, snapshots: dict[str, Snapshot | None], now: float, deadline: Callable[[], float], jev_blocked: bool = False) -> FunnelResult:
        """`deadline` returns the judge deadline on the judge client's clock."""
        res = FunnelResult()
        survivors = self._free_cut(snapshots, now, res)
        survivors = self._one_call(survivors, now, res)
        survivors = self._hard_facts(survivors, now, res)
        survivors = self._jev(survivors, now, deadline, jev_blocked, res)
        self._final_pick(survivors, now, res)
        return res

    def _reject(self, res: FunnelResult, now: float, symbol: str, stage: str, check: str, detail: str) -> None:
        rej = Rejection(symbol, stage, check, detail)
        res.rejections.append(rej)
        benched_until = None
        if check not in UNBENCHED_CHECKS:
            benched_until = self.bench.bench(symbol, check, now).until
        self.log.write("rejection", now, symbol=symbol, stage=stage, check=check, detail=detail, bot=self.battery.bot, benched_until=benched_until)

    def _free_cut(self, snapshots: dict[str, Snapshot | None], now: float, res: FunnelResult) -> list[Snapshot]:
        f = self.cfg.funnel
        out = []
        for symbol, snap in snapshots.items():
            if (b := self.bench.benched(symbol, now)) is not None:
                res.rejections.append(Rejection(symbol, "free_cut", "benched", f"{b.check} until {b.until:.0f}"))
                continue  # already logged when benched; not re-logged every block
            if snap is None:
                self._reject(res, now, symbol, "free_cut", "no_data", "no tick before decision time")
            elif not snap.has_book:
                self._reject(res, now, symbol, "free_cut", "no_book", "newest tick has no book (backfilled history only)")
            elif now - snap.data_ts > f.max_tick_age_s:
                self._reject(res, now, symbol, "free_cut", "stale_data", f"last tick {now - snap.data_ts:.1f}s old")
            elif any(v is None for v in snap.returns_bps.values()):
                self._reject(res, now, symbol, "free_cut", "insufficient_history", "a return window has no reference price")
            elif snap.spread_bps > f.max_spread_bps:
                self._reject(res, now, symbol, "free_cut", "spread_too_wide", f"{snap.spread_bps:.2f} bps > {f.max_spread_bps}")
            elif not f.min_vol_bps <= snap.rvol_bps <= f.max_vol_bps:
                self._reject(res, now, symbol, "free_cut", "vol_out_of_range", f"{snap.rvol_bps:.2f} bps outside [{f.min_vol_bps}, {f.max_vol_bps}]")
            else:
                out.append(snap)
        return out

    def _one_call(self, snaps: list[Snapshot], now: float, res: FunnelResult) -> list[Snapshot]:
        out = []
        for snap in snaps:
            depth = self.feed.top_depth_usd(snap.symbol, snap.data_ts)  # newest data before the decision
            if depth < self.cfg.funnel.min_top_depth_usd:
                self._reject(res, now, snap.symbol, "one_call", "thin_book", f"top depth ${depth:,.0f} < ${self.cfg.funnel.min_top_depth_usd:,.0f}")
            else:
                out.append(snap)
        return out

    def _hard_facts(self, snaps: list[Snapshot], now: float, res: FunnelResult) -> list[Snapshot]:
        out = []
        passive = self.battery.bot == "house"
        for snap in snaps:
            move = expected_move_bps(snap, self.cfg)
            cost = cost_bps(snap, self.cfg.fees, self.cfg.order_size_usd, passive, legs=2)
            if move < cost * self.cfg.risk.fee_edge_margin:
                self._reject(res, now, snap.symbol, "hard_facts", "costs_exceed_move", f"expected move {move:.2f} bps < costs {cost:.2f} x {self.cfg.risk.fee_edge_margin}")
            elif self.cfg.order_size_usd > self.cfg.risk.max_position_usd:
                self._reject(res, now, snap.symbol, "hard_facts", "order_exceeds_max_position", f"order ${self.cfg.order_size_usd} > max ${self.cfg.risk.max_position_usd}")
            else:
                out.append(snap)
        # Rank by expected move per unit of cost so the judge sees the best candidates first.
        return sorted(out, key=lambda s: expected_move_bps(s, self.cfg) / max(cost_bps(s, self.cfg.fees, self.cfg.order_size_usd, passive, 2), 1e-9), reverse=True)

    def _jev(self, snaps: list[Snapshot], now: float, deadline: Callable[[], float], blocked: bool, res: FunnelResult) -> list[Judged]:
        cap = self.cfg.funnel.max_jev_candidates
        judged: list[Judged] = []
        questions = self.battery.to_wire()
        for i, snap in enumerate(snaps):
            if blocked:
                self._reject(res, now, snap.symbol, "jev", "jev_cooldown", "backing off after a rate limit")
                continue
            if i >= cap:
                self._reject(res, now, snap.symbol, "jev", "jev_capacity", f"only {cap} candidates go to Jev per block")
                continue
            decision_id = f"{self.battery.bot}-{snap.symbol}-{int(now * 1000)}"
            state = snap.to_state()
            self.log.write("snapshot", now, decision_id=decision_id, snapshot=state, data_ts=snap.data_ts)
            self.log.write("question", now, decision_id=decision_id, bot=self.battery.bot, battery_version=self.battery.version, state=state, questions=questions)
            try:
                result = self.judge.ask(state, questions, deadline())
            except JudgeError as e:
                if isinstance(e, JudgeRateLimited):
                    res.rate_limited_for_s = e.retry_after_s or 5.0
                    blocked = True
                self.log.write("error", now, decision_id=decision_id, check=e.check, detail=str(e))
                self._reject(res, now, snap.symbol, "jev", e.check, str(e))
                continue
            self.log.write(
                "answer", now, decision_id=decision_id, bot=self.battery.bot, battery_version=self.battery.version,
                model=result.model, answers=result.answers, usage=result.usage, request_id=result.request_id, latency_s=round(result.latency_s, 4),
            )
            decision = decide(self.battery.bot, result.answers, snap, self.cfg, has_position=False)
            self.log.write("decision", now, decision_id=decision_id, model=result.model, mode=self.cfg.mode.value, **decision.to_log())
            judged.append(Judged(decision_id, snap, result, decision))
        res.judged = judged
        return judged

    def _final_pick(self, judged: list[Judged], now: float, res: FunnelResult) -> None:
        acting = []
        for j in judged:
            if j.decision.action == Action.HOLD:
                best = max(j.decision.scores.values(), key=lambda s: s.margin)
                self._reject(res, now, j.snapshot.symbol, "final_pick", f"below_threshold:{best.action.value}", f"p={best.p:.3f} < threshold {best.threshold:.3f}")
            else:
                acting.append(j)
        if not acting:
            return
        acting.sort(key=lambda j: j.decision.p - j.decision.threshold, reverse=True)
        res.pick = acting[0]
        for j in acting[1:]:
            # Lost the final pick to a better candidate: logged, not benched.
            res.rejections.append(Rejection(j.snapshot.symbol, "final_pick", "not_best", f"lost to {res.pick.snapshot.symbol}"))
            self.log.write("rejection", now, symbol=j.snapshot.symbol, stage="final_pick", check="not_best", detail=f"lost to {res.pick.snapshot.symbol}", bot=self.battery.bot, benched_until=None)
