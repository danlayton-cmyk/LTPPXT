"""The desk loop. One step per block.

    python -m desk.run --mode shadow --bot gambler --feed synthetic --judge fake --blocks 360

Per block: feed new ticks to the paper broker, build snapshots from data stamped before
the block, resolve outcomes that have come due, then either manage the open position
(no scanning) or run the funnel. The judge deadline is the start of the next block; a
decision that misses it holds.
"""

import argparse
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import replace

from book.book import Bench, Book, OpenPosition, PendingEntry
from broker import make_broker
from broker.base import Order
from desk.config import DeskConfig, Mode
from desk.feed import MarketFeed, ReplayFeed, SyntheticFeed
from filter.funnel import Funnel, Judged
from judge.client import JudgeClient, JudgeError, JudgeRateLimited
from judge.fake import FakeJudge
from logs.writer import EventLog, utc_day
from policy.policy import Action, cost_bps, decide
from questions import load_battery
from risk.stops import stop_hit
from risk.vetoes import GuardedBroker, RiskContext, VetoedOrder, check_order
from state.snapshot import AccountView, Snapshot, build_snapshot


class Desk:
    def __init__(self, cfg: DeskConfig, feed: MarketFeed, judge, log: EventLog, judge_clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg, self.feed, self.judge, self.log, self.judge_clock = cfg, feed, judge, log, judge_clock
        self.battery = load_battery(cfg.bot, cfg.outcome_horizon_s)
        self.book = Book()
        self.bench = Bench(cfg.bench_seconds)
        self.funnel = Funnel(cfg, self.battery, feed, judge, self.bench, log)
        self.paper = make_broker(cfg) if cfg.mode == Mode.PAPER else None
        self.broker = GuardedBroker(self.paper, cfg.risk, cfg.jev_model) if self.paper else None
        self.pending_outcomes: list[tuple[float, str, Snapshot]] = []
        self.prev_mid: dict[str, float] = {}
        self.fed_until = -float("inf")
        self.peak_equity = cfg.starting_cash_usd
        self.day: str | None = None
        self.day_start_equity = cfg.starting_cash_usd
        self.jev_cooldown_until = -float("inf")

    # -- accounting ------------------------------------------------------------------

    def _marks(self, now: float) -> dict[str, float]:
        marks = {}
        for s in self.cfg.symbols:
            ticks = self.feed.ticks(s, now, self.cfg.state.vol_window_s)
            if ticks:
                marks[s] = ticks[-1].mid
        return marks

    def _equity(self, marks: dict[str, float]) -> float:
        return self.paper.equity_usd(marks) if self.paper else self.cfg.starting_cash_usd

    def _inventory(self, symbol: str) -> float:
        return self.paper.position(symbol).qty if self.paper else 0.0

    def _gross_exposure(self, marks: dict[str, float]) -> float:
        if not self.paper:
            return 0.0
        return sum(abs(self.paper.position(s).qty) * marks.get(s, 0.0) for s in self.cfg.symbols)

    # -- the block -------------------------------------------------------------------

    def step(self, now: float) -> None:
        deadline = self.judge_clock() + self.cfg.block_interval_s
        self._feed_broker(now)
        marks = self._marks(now)
        equity = self._equity(marks)
        if utc_day(now) != self.day:
            self.day, self.day_start_equity = utc_day(now), equity
        self.peak_equity = max(self.peak_equity, equity)

        snaps = {s: self._snapshot(s, now, equity) for s in self.cfg.symbols}
        self._resolve_outcomes(now, snaps, equity)

        if self.book.position is not None:
            self._manage_position(now, snaps[self.book.position.symbol], marks, equity, deadline)
        elif self.book.pending is not None:
            self._manage_pending(now)
        else:
            blocked = self.judge_clock() < self.jev_cooldown_until
            res = self.funnel.run(snaps, now, lambda: deadline, jev_blocked=blocked)
            if res.rate_limited_for_s:
                self.jev_cooldown_until = self.judge_clock() + res.rate_limited_for_s
            for j in res.judged:
                self.pending_outcomes.append((now + self.cfg.outcome_horizon_s, j.decision_id, j.snapshot))
            if res.pick is not None:
                self._enter(now, res.pick, marks, equity)

        for s, snap in snaps.items():
            if snap is not None:
                self.prev_mid[s] = snap.mid
        pos = self.book.position
        self.log.write(
            "equity", now, mode=self.cfg.mode.value, bot=self.cfg.bot, equity_usd=round(equity, 4),
            cash_usd=round(self.paper.cash_usd(), 4) if self.paper else None,
            day_pnl_usd=round(equity - self.day_start_equity, 4),
            realized_pnl_usd=round(self.paper.realized_pnl_usd, 4) if self.paper else 0.0,
            position=None if pos is None else {"symbol": pos.symbol, "side": pos.side, "qty": pos.qty},
        )

    def _snapshot(self, symbol: str, now: float, equity: float) -> Snapshot | None:
        ticks = self.feed.ticks(symbol, now, self.cfg.state.vol_window_s + 1)
        account = AccountView(self._inventory(symbol), equity, self.peak_equity)
        return build_snapshot(ticks, now, account, self.cfg.state)

    def _feed_broker(self, now: float) -> None:
        if not self.paper:
            return
        lookback = now - self.fed_until if self.fed_until > -float("inf") else self.cfg.state.vol_window_s
        new = [t for s in self.cfg.symbols for t in self.feed.ticks(s, now, lookback) if t.ts > self.fed_until]
        for t in sorted(new, key=lambda t: t.ts):
            for fill in self.paper.on_tick(t):
                self.log.write("fill", t.ts, mode=self.cfg.mode.value, order_id=fill.order_id, symbol=fill.symbol, side=fill.side, qty=fill.qty, price=fill.price, fee_usd=fill.fee_usd, gas_usd=fill.gas_usd)
                if self.book.pending and fill.order_id == self.book.pending.order_id:
                    self.book.open(OpenPosition(fill.symbol, fill.side, fill.qty, fill.price, fill.ts, self.book.pending.decision_id))
        self.fed_until = now

    def _risk_ctx(self, snap: Snapshot, now: float, marks: dict[str, float], equity: float) -> RiskContext:
        return RiskContext(
            mid=snap.mid,
            prev_mid=self.prev_mid.get(snap.symbol),
            data_age_s=now - snap.data_ts,
            position_qty=self._inventory(snap.symbol),
            gross_exposure_usd=self._gross_exposure(marks),
            equity_usd=equity,
            day_pnl_usd=equity - self.day_start_equity,
        )

    def _submit(self, now: float, order: Order, snap: Snapshot, marks, equity, model: str | None, gross_edge: float | None, cost: float, decision_id: str, stop_exit: bool = False):
        """Run risk, then execute (paper) or log what would have happened (shadow)."""
        ctx = self._risk_ctx(snap, now, marks, equity)
        order_log = {"decision_id": decision_id, "order_id": order.id, "symbol": order.symbol, "side": order.side, "qty": order.qty,
                     "order_type": order.type, "limit_price": order.limit_price, "reduce_only": order.reduce_only, "mode": self.cfg.mode.value}
        if self.broker is None:  # shadow: same checks, nothing executes
            vetoes = check_order(order, ctx, self.cfg.risk, self.cfg.jev_model, model, gross_edge, cost, stop_exit)
            for v in vetoes:
                self.log.write("veto", now, check=v.check, detail=v.detail, **order_log)
            self.log.write("order", now, executed=False, would_pass_risk=not vetoes, **order_log)
            return None, bool(vetoes)
        try:
            fill = self.broker.submit(order, ctx, model, gross_edge, cost, stop_exit)
        except VetoedOrder as e:
            for v in e.vetoes:
                self.log.write("veto", now, check=v.check, detail=v.detail, **order_log)
            self.log.write("order", now, executed=False, would_pass_risk=False, **order_log)
            return None, True
        self.log.write("order", now, executed=True, would_pass_risk=True, **order_log)
        if fill is not None:
            self.log.write("fill", fill.ts, mode=self.cfg.mode.value, order_id=fill.order_id, symbol=fill.symbol, side=fill.side, qty=fill.qty, price=fill.price, fee_usd=fill.fee_usd, gas_usd=fill.gas_usd)
        return fill, False

    def _enter(self, now: float, pick: Judged, marks, equity) -> None:
        d, snap = pick.decision, pick.snapshot
        side = "buy" if d.action == Action.ENTER_LONG else "sell"
        qty = self.cfg.order_size_usd / snap.mid
        half = snap.mid * snap.spread_bps / 2e4
        limit = (snap.mid - half if side == "buy" else snap.mid + half) if d.passive else None
        order = Order(uuid.uuid4().hex[:12], snap.symbol, side, qty, "limit" if d.passive else "market", limit, False, now)
        fill, vetoed = self._submit(now, order, snap, marks, equity, pick.result.model, d.gross_edge_bps, d.cost_bps, pick.decision_id)
        if vetoed:
            self.bench.bench(snap.symbol, "risk_veto", now)
        elif fill is not None:
            self.book.open(OpenPosition(fill.symbol, fill.side, fill.qty, fill.price, fill.ts, pick.decision_id))
        elif self.broker is not None:
            self.book.pending = PendingEntry(order.id, order.symbol, pick.decision_id, now)

    def _manage_pending(self, now: float) -> None:
        p = self.book.pending
        if now - p.placed_ts >= self.cfg.block_interval_s:
            self.broker.inner.cancel(p.order_id)
            self.log.write("order", now, decision_id=p.decision_id, order_id=p.order_id, symbol=p.symbol, cancelled=True, executed=False, mode=self.cfg.mode.value)
            self.book.pending = None

    def _manage_position(self, now: float, snap: Snapshot | None, marks, equity, deadline: float) -> None:
        pos = self.book.position
        if snap is None:
            return  # no price to act on: hold
        adverse = stop_hit(pos.side, pos.entry_price, snap.mid, self.cfg.risk.stop_loss_bps)
        if adverse is not None:
            decision_id = f"{self.cfg.bot}-{pos.symbol}-stop-{int(now * 1000)}"
            self.log.write("stop_loss", now, decision_id=decision_id, symbol=pos.symbol, entry_price=pos.entry_price, mid=snap.mid,
                           adverse_bps=round(adverse, 3), stop_loss_bps=self.cfg.risk.stop_loss_bps, mode=self.cfg.mode.value)
            cost = cost_bps(snap, self.cfg.fees, self.cfg.order_size_usd, passive=False, legs=1)
            if self._exit(now, snap, marks, equity, None, cost, decision_id, "stop_loss"):
                self.bench.bench(pos.symbol, "stop_loss", now)
            return
        if self.judge_clock() < self.jev_cooldown_until:
            return  # hold
        decision_id = f"{self.cfg.bot}-{pos.symbol}-exit-{int(now * 1000)}"
        state, questions = snap.to_state(), self.battery.to_wire()
        self.log.write("snapshot", now, decision_id=decision_id, snapshot=state, data_ts=snap.data_ts)
        self.log.write("question", now, decision_id=decision_id, bot=self.cfg.bot, battery_version=self.battery.version, state=state, questions=questions)
        try:
            result = self.judge.ask(state, questions, deadline)
        except JudgeError as e:
            if isinstance(e, JudgeRateLimited):
                self.jev_cooldown_until = self.judge_clock() + (e.retry_after_s or 5.0)
            self.log.write("error", now, decision_id=decision_id, check=e.check, detail=f"holding position: {e}")
            return
        self.log.write("answer", now, decision_id=decision_id, bot=self.cfg.bot, battery_version=self.battery.version, model=result.model,
                       answers=result.answers, usage=result.usage, request_id=result.request_id, latency_s=round(result.latency_s, 4))
        d = decide(self.cfg.bot, result.answers, snap, self.cfg, has_position=True)
        self.log.write("decision", now, decision_id=decision_id, model=result.model, mode=self.cfg.mode.value, **d.to_log())
        self.pending_outcomes.append((now + self.cfg.outcome_horizon_s, decision_id, snap))
        if d.action == Action.EXIT:
            self._exit(now, snap, marks, equity, result.model, d.cost_bps, decision_id, "policy")

    def _exit(self, now: float, snap: Snapshot, marks, equity, model: str | None, cost: float, decision_id: str, reason: str) -> bool:
        """Close the open position with a reduce-only market order through risk. True if it closed."""
        pos = self.book.position
        side = "sell" if pos.side == "buy" else "buy"
        order = Order(uuid.uuid4().hex[:12], pos.symbol, side, pos.qty, "market", None, True, now)
        fill, _ = self._submit(now, order, snap, marks, equity, model, None, cost, decision_id, stop_exit=reason == "stop_loss")
        if fill is None:
            return False
        closed = self.book.close()
        sign = 1 if closed.side == "buy" else -1
        self.log.write("trade_closed", now, decision_id=closed.decision_id, exit_decision_id=decision_id, exit_reason=reason, symbol=closed.symbol,
                       entry_price=closed.entry_price, exit_price=fill.price, qty=closed.qty, gross_pnl_usd=round(sign * (fill.price - closed.entry_price) * closed.qty, 6))
        return True

    def _resolve_outcomes(self, now: float, snaps: dict[str, Snapshot | None], equity: float) -> None:
        due = [p for p in self.pending_outcomes if p[0] <= now]
        self.pending_outcomes = [p for p in self.pending_outcomes if p[0] > now]
        for _, decision_id, then in due:
            later = snaps.get(then.symbol)
            if later is None:
                continue
            outcomes = {}
            for q in self.battery.questions:
                if q.resolver is not None:
                    outcomes[q.id] = q.resolver(then, later)
            self.log.write("outcome", now, decision_id=decision_id, symbol=then.symbol, battery_version=self.battery.version,
                           mid_then=then.mid, mid_now=later.mid, outcomes=outcomes)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Paper-only AI trading desk")
    ap.add_argument("--mode", choices=[m.value for m in Mode], default="shadow")
    ap.add_argument("--bot", choices=["gambler", "house", "spy"], default="gambler")
    ap.add_argument("--feed", choices=["synthetic", "replay"], default="synthetic")
    ap.add_argument("--replay-file")
    ap.add_argument("--judge", choices=["http", "fake"], default="http")
    ap.add_argument("--blocks", type=int, default=360)
    ap.add_argument("--log-dir", default="logs/data")
    args = ap.parse_args(argv)

    if os.environ.get("TYPESAFE_API_KEY"):
        print("refusing to start: TYPESAFE_API_KEY is set in the desk's environment. Only the judge may hold it.", file=sys.stderr)
        return 2

    cfg = replace(DeskConfig(), mode=Mode(args.mode), bot=args.bot, log_dir=args.log_dir)
    if args.feed == "replay":
        if not args.replay_file:
            ap.error("--replay-file is required with --feed replay")
        feed = ReplayFeed(args.replay_file)
        cfg = replace(cfg, symbols=feed.symbols)
    else:
        feed = SyntheticFeed(cfg.symbols, start_ts=1_760_000_000, seconds=cfg.state.vol_window_s + int(args.blocks * cfg.block_interval_s) + 60)

    secret = os.environ.get("DESK_JUDGE_SECRET", "")
    judge = FakeJudge() if args.judge == "fake" else JudgeClient(cfg.judge_url, secret)
    log = EventLog(cfg.log_dir, secrets=[secret])
    desk = Desk(cfg, feed, judge, log)

    # Offline feeds run in simulated time; a live feed would step on the wall clock instead.
    start = feed.first_ts() + cfg.state.vol_window_s + 1
    for i in range(args.blocks):
        desk.step(start + i * cfg.block_interval_s)
    print(f"{args.blocks} blocks done, mode={cfg.mode.value}, bot={cfg.bot}, logs in {cfg.log_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
