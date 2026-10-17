"""Broker interface. A live broker would implement exactly this; this repo ships none."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]


@dataclass(frozen=True)
class Order:
    id: str
    symbol: str
    side: Side
    qty: float  # base units, always positive
    type: OrderType
    limit_price: float | None
    reduce_only: bool
    created_ts: float


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee_usd: float
    gas_usd: float
    ts: float


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float  # signed
    avg_price: float


class Broker(ABC):
    is_live: bool = False

    @abstractmethod
    def submit(self, order: Order) -> Fill | None:
        """Place an order. Returns the fill if it filled immediately."""

    @abstractmethod
    def cancel(self, order_id: str) -> bool: ...

    @abstractmethod
    def open_orders(self) -> list[Order]: ...

    @abstractmethod
    def position(self, symbol: str) -> Position: ...

    @abstractmethod
    def cash_usd(self) -> float: ...

    @abstractmethod
    def equity_usd(self, marks: dict[str, float]) -> float: ...
