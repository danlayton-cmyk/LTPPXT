"""Brokers. Paper only: make_broker() cannot return anything that touches real money."""

from broker.base import Broker, Fill, Order, Position
from broker.paper import PaperBroker
from desk.config import DeskConfig


def make_broker(cfg: DeskConfig) -> PaperBroker:
    broker = PaperBroker(cfg.starting_cash_usd, cfg.fees)
    if broker.is_live:
        raise RuntimeError("refusing to start: broker reports is_live")
    return broker


__all__ = ["Broker", "Fill", "Order", "PaperBroker", "Position", "make_broker"]
