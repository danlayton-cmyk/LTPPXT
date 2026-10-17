"""Hard vetoes, checked before every order.

These are plain code over plain numbers. Nothing a model returns can change a limit or
skip a check: the inputs are prices, positions and config, plus the model version string,
which must equal the pinned one exactly.

Reduce-only orders (exits) skip the exposure checks (max position, daily loss, leverage,
fees-plus-gas), because blocking an exit would trap risk on the book. They still face the
range guard and, when a model triggered them, the model version check.
"""

from dataclasses import dataclass

from broker.base import Broker, Fill, Order
from desk.config import RiskConfig


@dataclass(frozen=True)
class RiskContext:
    mid: float
    prev_mid: float | None  # previous block's mid, for the bad-data guard
    data_age_s: float
    position_qty: float  # signed, this symbol
    gross_exposure_usd: float  # all symbols, at marks
    equity_usd: float
    day_pnl_usd: float  # equity change since the UTC day started, fees included


@dataclass(frozen=True)
class Veto:
    check: str
    detail: str


def check_order(
    order: Order,
    ctx: RiskContext,
    cfg: RiskConfig,
    pinned_model: str,
    model_version: str | None,
    gross_edge_bps: float | None,
    cost_bps: float,
) -> list[Veto]:
    """Return every veto that fires. An empty list means the order may go.

    gross_edge_bps is the expected move in the order's favor before costs; cost_bps is the
    round trip of fees, gas and spread. Both are computed in code from the snapshot.
    """
    vetoes: list[Veto] = []
    price = order.limit_price if order.limit_price is not None else ctx.mid
    notional = order.qty * price
    signed = order.qty if order.side == "buy" else -order.qty

    # Model version: a model-originated order must come from exactly the pinned model.
    if model_version is not None and model_version != pinned_model:
        vetoes.append(Veto("model_version", f"answer came from {model_version!r}, pinned is {pinned_model!r}"))
    if model_version is None and not order.reduce_only:
        vetoes.append(Veto("model_version", "opening order has no model version attached"))

    # Range guard: sane, fresh data and an order price near the market.
    if ctx.data_age_s > cfg.max_data_age_s:
        vetoes.append(Veto("range_guard", f"data is {ctx.data_age_s:.1f}s old, limit {cfg.max_data_age_s}s"))
    if ctx.prev_mid and abs(ctx.mid / ctx.prev_mid - 1) * 1e4 > cfg.max_mid_jump_bps:
        vetoes.append(Veto("range_guard", f"mid jumped {abs(ctx.mid / ctx.prev_mid - 1) * 1e4:.0f} bps since last block"))
    if abs(price / ctx.mid - 1) * 1e4 > cfg.range_guard_bps:
        vetoes.append(Veto("range_guard", f"order price {price} is {abs(price / ctx.mid - 1) * 1e4:.0f} bps from mid"))

    if order.reduce_only:
        return vetoes

    new_pos_usd = abs(ctx.position_qty + signed) * ctx.mid
    if new_pos_usd > cfg.max_position_usd:
        vetoes.append(Veto("max_position", f"position would be ${new_pos_usd:,.2f}, limit ${cfg.max_position_usd:,.2f}"))
    if ctx.day_pnl_usd <= -cfg.daily_loss_limit_usd:
        vetoes.append(Veto("daily_loss_limit", f"day PnL ${ctx.day_pnl_usd:,.2f}, limit -${cfg.daily_loss_limit_usd:,.2f}"))
    if ctx.equity_usd <= 0:
        vetoes.append(Veto("leverage_cap", "equity is zero or negative"))
    else:
        lev = (ctx.gross_exposure_usd + notional) / ctx.equity_usd
        if lev > cfg.max_leverage:
            vetoes.append(Veto("leverage_cap", f"leverage would be {lev:.2f}x, cap {cfg.max_leverage}x"))
    if gross_edge_bps is None or gross_edge_bps < cost_bps * cfg.fee_edge_margin:
        vetoes.append(Veto("fees_plus_gas", f"gross edge {gross_edge_bps} bps does not cover fees+gas+spread {cost_bps:.2f} bps x {cfg.fee_edge_margin}"))
    return vetoes


class VetoedOrder(Exception):
    def __init__(self, vetoes: list[Veto]) -> None:
        super().__init__("; ".join(f"{v.check}: {v.detail}" for v in vetoes))
        self.vetoes = vetoes


class GuardedBroker:
    """The only object the desk uses to place orders. Every submit runs check_order first."""

    def __init__(self, broker: Broker, cfg: RiskConfig, pinned_model: str) -> None:
        if broker.is_live:
            raise RuntimeError("refusing to wrap a live broker")
        self._broker = broker
        self._cfg = cfg
        self._pinned = pinned_model

    @property
    def inner(self) -> Broker:
        return self._broker

    def submit(self, order: Order, ctx: RiskContext, model_version: str | None, gross_edge_bps: float | None, cost_bps: float) -> Fill | None:
        vetoes = check_order(order, ctx, self._cfg, self._pinned, model_version, gross_edge_bps, cost_bps)
        if vetoes:
            raise VetoedOrder(vetoes)
        return self._broker.submit(order)
