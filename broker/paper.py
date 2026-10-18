"""Paper broker. Simulates fills against the latest ticks. Touches no network and no money."""

from broker.base import Broker, Fill, Order, Position
from desk.config import FeeConfig
from state.snapshot import Tick


class PaperBroker(Broker):
    is_live = False

    def __init__(self, starting_cash_usd: float, fees: FeeConfig) -> None:
        self._cash = starting_cash_usd
        self._fees = fees
        self._positions: dict[str, Position] = {}
        self._last: dict[str, Tick] = {}
        self._resting: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.realized_pnl_usd = 0.0  # after fees and gas

    def on_tick(self, tick: Tick) -> list[Fill]:
        """Feed a new tick. Resting limit orders fill at their limit when the far side reaches them."""
        self._last[tick.symbol] = tick
        filled = []
        for order in list(self._resting.values()):
            if order.symbol != tick.symbol or order.limit_price is None:
                continue
            crossed = tick.ask <= order.limit_price if order.side == "buy" else tick.bid >= order.limit_price
            if crossed:
                del self._resting[order.id]
                filled.append(self._fill(order, order.limit_price, self._fees.maker_fee_bps, tick.ts))
        return filled

    def submit(self, order: Order) -> Fill | None:
        tick = self._last.get(order.symbol)
        if tick is None:
            raise ValueError(f"no market data for {order.symbol}")
        if order.qty <= 0:
            raise ValueError("qty must be positive")
        if order.type == "market":
            slip = self._fees.slippage_bps / 1e4
            price = tick.ask * (1 + slip) if order.side == "buy" else tick.bid * (1 - slip)
            return self._fill(order, price, self._fees.taker_fee_bps, tick.ts)
        if order.limit_price is None:
            raise ValueError("limit order needs a limit_price")
        marketable = order.limit_price >= tick.ask if order.side == "buy" else order.limit_price <= tick.bid
        if marketable:
            price = tick.ask if order.side == "buy" else tick.bid
            return self._fill(order, price, self._fees.taker_fee_bps, tick.ts)
        self._resting[order.id] = order
        return None

    def cancel(self, order_id: str) -> bool:
        return self._resting.pop(order_id, None) is not None

    def open_orders(self) -> list[Order]:
        return list(self._resting.values())

    def position(self, symbol: str) -> Position:
        return self._positions.get(symbol, Position(symbol, 0.0, 0.0))

    def cash_usd(self) -> float:
        return self._cash

    def equity_usd(self, marks: dict[str, float]) -> float:
        return self._cash + sum(p.qty * marks.get(s, p.avg_price) for s, p in self._positions.items())

    def _fill(self, order: Order, price: float, fee_bps: float, ts: float) -> Fill:
        qty = order.qty
        pos = self.position(order.symbol)
        if order.reduce_only:
            reducing = (pos.qty > 0 and order.side == "sell") or (pos.qty < 0 and order.side == "buy")
            if not reducing:
                raise ValueError("reduce-only order would increase the position")
            qty = min(qty, abs(pos.qty))
        signed = qty if order.side == "buy" else -qty
        notional = qty * price
        fee = notional * fee_bps / 1e4
        gas = self._fees.gas_usd_per_fill
        self._cash -= signed * price + fee + gas
        self.realized_pnl_usd -= fee + gas

        new_qty = pos.qty + signed
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            avg = (pos.avg_price * abs(pos.qty) + price * qty) / abs(new_qty)
        else:
            closed = min(abs(signed), abs(pos.qty))
            self.realized_pnl_usd += closed * (price - pos.avg_price) * (1 if pos.qty > 0 else -1)
            avg = pos.avg_price if abs(new_qty) > 0 and (new_qty > 0) == (pos.qty > 0) else price
        if abs(new_qty) < 1e-12:
            self._positions.pop(order.symbol, None)
        else:
            self._positions[order.symbol] = Position(order.symbol, new_qty, avg)

        fill = Fill(order.id, order.symbol, order.side, qty, price, fee, gas, ts)
        self.fills.append(fill)
        return fill
