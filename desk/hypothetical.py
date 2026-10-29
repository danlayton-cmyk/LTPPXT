"""Hypothetical trades for shadow mode. Simulated only: nothing here executes or touches money.

Shadow mode logs the orders the policy would place but holds nothing, so its would-be
trades have no P&L. This ledger gives them one. Each would-be entry that passed risk goes
to a PaperBroker of the ledger's own, fed the same ticks under the same fees and fill
rules as paper mode, and the position is closed by code:

- the stop-loss, at the same stop_loss_bps and under the same stop-exit risk rules, or
- the bot's holding period (DeskConfig.horizon_s), the horizon its entry was priced for.

Paper mode closes on the Jev exit question instead. Asking it here would add a judge call
every block while a hypothetical position is open, so this ledger never asks Jev.

Like the book, it holds one position or resting entry at a time, and takes no entry in a
block that started with one open. A would-be entry that arrives then is logged as skipped,
so shadow re-asking a symbol every block cannot stack duplicate trades. Risk runs again
against the ledger's own equity and day P&L, so the daily loss limit binds as in paper.

Everything is logged under hypothetical_* event types, which no other report reads as a
fill or a trade. The desk's scan, bench and judge calls are unchanged. The ledger lives in
memory: a restart starts it flat at starting cash, like the paper broker.
"""

import math
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from book.book import Bench, Book, OpenPosition, PendingEntry
from broker.base import Fill, Order
from broker.paper import PaperBroker
from desk.config import DeskConfig
from desk.feed import MarketFeed
from filter.funnel import Judged
from logs.writer import EventLog, utc_day
from policy.policy import cost_bps
from risk.stops import stop_hit
from risk.vetoes import RiskContext, check_order
from state.snapshot import Snapshot


class HypotheticalLedger:
    def __init__(self, cfg: DeskConfig, feed: MarketFeed, log: EventLog) -> None:
        self.cfg, self.feed, self.log = cfg, feed, log
        self.broker = PaperBroker(cfg.starting_cash_usd, cfg.fees)
        self.book = Book()
        self.bench = Bench(cfg.bench_seconds)  # the ledger's own; the desk's bench never sees it
        self.entry_costs_usd = 0.0  # fee + gas of the open position's entry fill
        self.busy: str | None = None  # what was open when this block started
        self.fed_until = -math.inf
        self.day: str | None = None
        self.day_start_equity = cfg.starting_cash_usd

    # -- called by the desk ----------------------------------------------------------

    def step(self, now: float, snaps: dict[str, Snapshot | None], marks: dict[str, float], prev_mid: dict[str, float]) -> None:
        """Once per block, before the scan: fill resting entries, then manage what is open."""
        with self._contained(now):
            self._feed(now)
            equity = self.broker.equity_usd(marks)
            if utc_day(now) != self.day:
                self.day, self.day_start_equity = utc_day(now), equity
            self.busy = "position_open" if self.book.position else "entry_resting" if self.book.pending else None
            if self.book.position is not None:
                self._manage_position(now, snaps.get(self.book.position.symbol), marks, prev_mid)
            elif self.book.pending is not None:
                self._manage_pending(now)

    def enter(self, now: float, order: Order, pick: Judged, marks: dict[str, float], prev_mid: dict[str, float]) -> None:
        """Offer a would-be entry that passed the desk's risk checks."""
        with self._contained(now):
            fields = self._order_fields(order, pick.decision_id, "entry")
            if self.busy is not None:
                self.log.write("hypothetical_order", now, status="skipped", reason=self.busy, **fields)
                return
            if (b := self.bench.benched(order.symbol, now)) is not None:
                self.log.write("hypothetical_order", now, status="skipped", reason=f"benched:{b.check}", **fields)
                return
            d = pick.decision
            vetoes = check_order(order, self._risk_ctx(pick.snapshot, now, marks, prev_mid), self.cfg.risk, self.cfg.jev_model,
                                 pick.result.model, d.gross_edge_bps, d.cost_bps)
            if vetoes:
                self.bench.bench(order.symbol, "risk_veto", now)
                self.log.write("hypothetical_order", now, status="vetoed", reason="; ".join(f"{v.check}: {v.detail}" for v in vetoes), **fields)
                return
            fill = self.broker.submit(order)
            if fill is None:
                self.book.pending = PendingEntry(order.id, order.symbol, pick.decision_id, now)
                self.log.write("hypothetical_order", now, status="resting", **fields)
                return
            self.log.write("hypothetical_order", now, status="filled", **fields)
            self._opened(fill, pick.decision_id)

    # -- internals -------------------------------------------------------------------

    @contextmanager
    def _contained(self, now: float) -> Iterator[None]:
        """A ledger failure is logged and swallowed: it must never cost shadow mode a block."""
        try:
            yield
        except Exception as e:
            self.log.write("hypothetical_error", now, detail=f"{type(e).__name__}: {e}")

    def _feed(self, now: float) -> None:
        lookback = now - self.fed_until if self.fed_until > -math.inf else self.cfg.state.vol_window_s
        new = [t for s in self.cfg.symbols for t in self.feed.ticks(s, now, lookback) if t.ts > self.fed_until]
        for t in sorted(new, key=lambda t: t.ts):
            for fill in self.broker.on_tick(t):  # only entries rest, so every fill here is one
                if self.book.pending and fill.order_id == self.book.pending.order_id:
                    self._opened(fill, self.book.pending.decision_id)
        self.fed_until = now

    def _opened(self, fill: Fill, decision_id: str) -> None:
        self._log_fill(fill, decision_id, "entry")
        self.book.open(OpenPosition(fill.symbol, fill.side, fill.qty, fill.price, fill.ts, decision_id))
        self.entry_costs_usd = fill.fee_usd + fill.gas_usd

    def _manage_pending(self, now: float) -> None:
        p = self.book.pending
        if now - p.placed_ts >= self.cfg.block_interval_s:
            self.broker.cancel(p.order_id)
            self.log.write("hypothetical_order", now, status="cancelled", reason="unfilled after one block",
                           decision_id=p.decision_id, order_id=p.order_id, symbol=p.symbol, role="entry")
            self.book.pending = None

    def _manage_position(self, now: float, snap: Snapshot | None, marks: dict[str, float], prev_mid: dict[str, float]) -> None:
        pos = self.book.position
        if snap is None:
            return  # no price to act on: hold
        adverse = stop_hit(pos.side, pos.entry_price, snap.mid, self.cfg.risk.stop_loss_bps)
        if adverse is not None:
            if self._exit(now, snap, marks, prev_mid, "stop_loss", adverse_bps=round(adverse, 3)):
                self.bench.bench(pos.symbol, "stop_loss", now)
        elif now - pos.entry_ts >= self.cfg.horizon_s:
            self._exit(now, snap, marks, prev_mid, "holding_period")

    def _exit(self, now: float, snap: Snapshot, marks: dict[str, float], prev_mid: dict[str, float], reason: str, **extra) -> bool:
        """Close the position with a reduce-only market order through risk, as the desk's exit does. True if it closed."""
        pos = self.book.position
        side = "sell" if pos.side == "buy" else "buy"
        order = Order(uuid.uuid4().hex[:12], pos.symbol, side, pos.qty, "market", None, True, now)
        fields = self._order_fields(order, pos.decision_id, "exit")
        cost = cost_bps(snap, self.cfg.fees, self.cfg.order_size_usd, passive=False, legs=1)
        vetoes = check_order(order, self._risk_ctx(snap, now, marks, prev_mid), self.cfg.risk, self.cfg.jev_model, None, None, cost,
                             stop_exit=reason == "stop_loss")
        if vetoes:
            self.log.write("hypothetical_order", now, status="vetoed", reason="; ".join(f"{v.check}: {v.detail}" for v in vetoes), exit_reason=reason, **fields)
            return False
        fill = self.broker.submit(order)
        self.log.write("hypothetical_order", now, status="filled", exit_reason=reason, **fields)
        self._log_fill(fill, pos.decision_id, "exit")
        closed = self.book.close()
        sign = 1 if closed.side == "buy" else -1
        notional = closed.entry_price * closed.qty
        gross = sign * (fill.price - closed.entry_price) * closed.qty
        costs = self.entry_costs_usd + fill.fee_usd + fill.gas_usd
        self.log.write(
            "hypothetical_trade_closed", now, decision_id=closed.decision_id, exit_reason=reason, symbol=closed.symbol, side=closed.side,
            qty=closed.qty, entry_ts=closed.entry_ts, entry_price=closed.entry_price, exit_price=fill.price, held_s=round(now - closed.entry_ts, 3),
            notional_usd=round(notional, 6), gross_pnl_usd=round(gross, 6), costs_usd=round(costs, 6), net_pnl_usd=round(gross - costs, 6),
            net_bps=round((gross - costs) / notional * 1e4, 3), mode=self.cfg.mode.value, **extra,
        )
        self.entry_costs_usd = 0.0
        return True

    def _risk_ctx(self, snap: Snapshot, now: float, marks: dict[str, float], prev_mid: dict[str, float]) -> RiskContext:
        equity = self.broker.equity_usd(marks)
        return RiskContext(
            mid=snap.mid,
            prev_mid=prev_mid.get(snap.symbol),
            data_age_s=now - snap.data_ts,
            position_qty=self.broker.position(snap.symbol).qty,
            gross_exposure_usd=sum(abs(self.broker.position(s).qty) * marks.get(s, 0.0) for s in self.cfg.symbols),
            equity_usd=equity,
            day_pnl_usd=equity - self.day_start_equity,
        )

    def _order_fields(self, order: Order, decision_id: str, role: str) -> dict:
        return {"decision_id": decision_id, "order_id": order.id, "symbol": order.symbol, "side": order.side, "qty": order.qty, "order_type": order.type,
                "limit_price": order.limit_price, "role": role, "mode": self.cfg.mode.value}

    def _log_fill(self, fill: Fill, decision_id: str, role: str) -> None:
        self.log.write("hypothetical_fill", fill.ts, decision_id=decision_id, role=role, order_id=fill.order_id, symbol=fill.symbol, side=fill.side,
                       qty=fill.qty, price=fill.price, fee_usd=fill.fee_usd, gas_usd=fill.gas_usd, mode=self.cfg.mode.value)
