"""The book: at most one open position, and a bench for rejected candidates.

While a position or an entry order is open, the desk does not scan for new candidates;
it only manages the open position.
"""

from dataclasses import dataclass, field

from broker.base import Side


@dataclass
class OpenPosition:
    symbol: str
    side: Side  # side of the entry fill
    qty: float
    entry_price: float
    entry_ts: float
    decision_id: str


@dataclass(frozen=True)
class PendingEntry:
    order_id: str
    symbol: str
    decision_id: str
    placed_ts: float


@dataclass
class Book:
    position: OpenPosition | None = None
    pending: PendingEntry | None = None

    def can_scan(self) -> bool:
        return self.position is None and self.pending is None

    def open(self, position: OpenPosition) -> None:
        if self.position is not None:
            raise RuntimeError(f"one position at a time: {self.position.symbol} is already open")
        self.position = position
        self.pending = None

    def close(self) -> OpenPosition:
        if self.position is None:
            raise RuntimeError("no open position")
        closed, self.position = self.position, None
        return closed


@dataclass(frozen=True)
class BenchEntry:
    symbol: str
    check: str  # the check that rejected it
    until: float


@dataclass
class Bench:
    seconds_by_check: dict[str, float]
    default_seconds: float = 60.0
    entries: dict[str, BenchEntry] = field(default_factory=dict)

    def bench(self, symbol: str, check: str, now: float) -> BenchEntry:
        """Bench a symbol for as long as the rejecting check calls for. A longer existing
        benching is kept; a shorter one is extended."""
        until = now + self.seconds_by_check.get(_base_check(check), self.default_seconds)
        current = self.entries.get(symbol)
        if current is None or until > current.until:
            self.entries[symbol] = BenchEntry(symbol, check, until)
        return self.entries[symbol]

    def benched(self, symbol: str, now: float) -> BenchEntry | None:
        entry = self.entries.get(symbol)
        if entry is not None and entry.until <= now:
            del self.entries[symbol]
            return None
        return entry


def _base_check(check: str) -> str:
    """'below_threshold:enter_long' benches like 'below_threshold'."""
    return check.split(":", 1)[0]
