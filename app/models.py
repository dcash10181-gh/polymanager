"""Domain models shared across the whole pipeline.

These pydantic models are the contract between modules: discovery produces
``Market``, the data layer produces ``OrderBook`` / ``MarketState``, the signal
engine produces ``Signal``, the reasoner produces ``ClaudeReview``, the risk
manager produces ``RiskDecision``, and the execution layer consumes
``OrderIntent`` and produces ``Order`` / ``Fill`` / ``Position``.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum

from pydantic import BaseModel, Field, computed_field

from app.config import StrategyName


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class Urgency(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


# --------------------------------------------------------------------------
# Market metadata (from Gamma)
# --------------------------------------------------------------------------
class Token(BaseModel):
    token_id: str
    outcome: str           # e.g. "Yes" / "No" / "Lakers"
    price: float | None = None  # last known outcome price (0..1)


class Market(BaseModel):
    """A tradeable market with its outcome tokens."""

    id: str
    condition_id: str | None = None
    question: str
    slug: str | None = None
    description: str = ""
    resolution_source: str = ""
    tokens: list[Token] = Field(default_factory=list)
    end_date: str | None = None          # ISO8601
    end_timestamp: float | None = None   # epoch seconds
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    reported_spread: float | None = None
    active: bool = True
    closed: bool = False
    neg_risk: bool = False               # multi-outcome neg-risk market
    tags: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[misc]
    @property
    def token_ids(self) -> list[str]:
        return [t.token_id for t in self.tokens]

    def token_for_outcome(self, outcome: str) -> Token | None:
        for t in self.tokens:
            if t.outcome.lower() == outcome.lower():
                return t
        return None

    @property
    def is_binary(self) -> bool:
        return len(self.tokens) == 2

    def seconds_to_resolution(self, now: float | None = None) -> float | None:
        if self.end_timestamp is None:
            return None
        return self.end_timestamp - (now if now is not None else time.time())


# --------------------------------------------------------------------------
# Order book (from CLOB)
# --------------------------------------------------------------------------
class BookLevel(BaseModel):
    price: float
    size: float  # in shares


class OrderBook(BaseModel):
    """A point-in-time order book snapshot for one token.

    ``bids``/``asks`` may arrive in any order from the API; all derived
    quantities below are computed defensively (max bid, min ask) so we never
    depend on the wire ordering.
    """

    token_id: str
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    timestamp: float = Field(default_factory=time.time)

    @property
    def best_bid(self) -> float | None:
        return max((b.price for b in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((a.price for a in self.asks), default=None)

    @property
    def midpoint(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    @property
    def spread(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return ba - bb

    def age_seconds(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.timestamp

    def is_stale(self, max_age: float, now: float | None = None) -> bool:
        return self.age_seconds(now) > max_age

    def has_two_sides(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    def bid_depth_usd(self, within: float = 0.0) -> float:
        """USD notional resting on the bid within ``within`` of the best bid."""
        bb = self.best_bid
        if bb is None:
            return 0.0
        floor = bb - within
        return sum(b.price * b.size for b in self.bids if b.price >= floor - 1e-12)

    def ask_depth_usd(self, within: float = 0.0) -> float:
        """USD notional resting on the ask within ``within`` of the best ask."""
        ba = self.best_ask
        if ba is None:
            return 0.0
        ceil = ba + within
        return sum(a.price * a.size for a in self.asks if a.price <= ceil + 1e-12)

    def imbalance(self) -> float | None:
        """(bid_depth - ask_depth) / (bid_depth + ask_depth) in [-1, 1]."""
        bd = self.bid_depth_usd()
        ad = self.ask_depth_usd()
        total = bd + ad
        if total <= 0:
            return None
        return (bd - ad) / total


# --------------------------------------------------------------------------
# Market state (rolling, with price velocities)
# --------------------------------------------------------------------------
class PricePoint(BaseModel):
    t: float
    mid: float


class MarketState(BaseModel):
    """Rolling per-token state used to compute velocities / staleness."""

    token_id: str
    book: OrderBook | None = None
    history: list[PricePoint] = Field(default_factory=list)
    last_update: float = Field(default_factory=time.time)

    def record(self, book: OrderBook) -> None:
        self.book = book
        self.last_update = book.timestamp
        mid = book.midpoint
        if mid is not None:
            self.history.append(PricePoint(t=book.timestamp, mid=mid))
            # keep ~2 minutes of history
            cutoff = book.timestamp - 120
            self.history = [p for p in self.history if p.t >= cutoff]

    def velocity(self, window_seconds: float, now: float | None = None) -> float | None:
        """Mid price change per second over the trailing window."""
        if len(self.history) < 2:
            return None
        now = now if now is not None else self.last_update
        recent = [p for p in self.history if p.t >= now - window_seconds]
        if len(recent) < 2:
            return None
        dt = recent[-1].t - recent[0].t
        if dt <= 0:
            return None
        return (recent[-1].mid - recent[0].mid) / dt


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------
class Signal(BaseModel):
    market_id: str
    token_id: str
    strategy: StrategyName
    side: Side
    estimated_fair_price: float
    best_bid: float | None = None
    best_ask: float | None = None
    edge: float
    confidence: float = 0.5
    urgency: Urgency = Urgency.medium
    max_price: float                 # worst price we will accept (buy: cap; sell: floor)
    suggested_size_usd: float
    take_profit_price: float | None = None
    stop_price: float | None = None
    time_stop_seconds: int | None = None
    risk_flags: list[str] = Field(default_factory=list)
    rationale: str = ""
    # arbitrage / multi-leg support
    legs: list["SignalLeg"] = Field(default_factory=list)

    def passes_basic_filters(self, min_edge: float, max_spread: float) -> tuple[bool, str]:
        if self.edge < min_edge:
            return False, f"edge {self.edge:.4f} < min_edge {min_edge:.4f}"
        if self.best_bid is not None and self.best_ask is not None:
            spread = self.best_ask - self.best_bid
            if spread > max_spread:
                return False, f"spread {spread:.4f} > max_spread {max_spread:.4f}"
        if self.suggested_size_usd <= 0:
            return False, "non-positive size"
        if not (0 < self.max_price < 1):
            return False, "max_price out of (0,1)"
        return True, "ok"


class SignalLeg(BaseModel):
    """One leg of a multi-leg (arbitrage / basket) signal."""

    token_id: str
    side: Side
    price: float
    size_usd: float


# --------------------------------------------------------------------------
# Claude review
# --------------------------------------------------------------------------
class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class ClaudeReview(BaseModel):
    allow_trade: bool = False
    risk_level: RiskLevel = RiskLevel.high
    resolution_ambiguity: bool = True
    main_risks: list[str] = Field(default_factory=list)
    confidence_adjustment: float = 0.0
    comment: str = ""
    valid_json: bool = True   # set False when the model returned unparseable output


# --------------------------------------------------------------------------
# Order intents / orders / fills / positions
# --------------------------------------------------------------------------
def new_client_order_id() -> str:
    return f"pm-{uuid.uuid4().hex[:16]}"


class OrderIntent(BaseModel):
    market_id: str
    token_id: str
    side: Side
    price: float
    size_usd: float
    strategy: StrategyName
    take_profit_price: float | None = None
    stop_price: float | None = None
    time_stop_seconds: int | None = None
    client_order_id: str = Field(default_factory=new_client_order_id)

    @property
    def size_shares(self) -> float:
        if self.price <= 0:
            return 0.0
        return round(self.size_usd / self.price, 2)


class OrderStatus(str, Enum):
    pending = "pending"
    open = "open"
    partially_filled = "partially_filled"
    filled = "filled"
    canceled = "canceled"
    rejected = "rejected"


class Order(BaseModel):
    client_order_id: str
    exchange_order_id: str | None = None
    token_id: str
    side: Side
    price: float
    size_shares: float
    filled_shares: float = 0.0
    status: OrderStatus = OrderStatus.pending
    strategy: StrategyName
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    reject_reason: str | None = None

    @property
    def remaining_shares(self) -> float:
        return max(0.0, self.size_shares - self.filled_shares)


class Fill(BaseModel):
    client_order_id: str
    token_id: str
    side: Side
    price: float
    size_shares: float
    fee_usd: float = 0.0
    timestamp: float = Field(default_factory=time.time)
    is_paper: bool = True

    @property
    def notional_usd(self) -> float:
        return self.price * self.size_shares


class Position(BaseModel):
    token_id: str
    market_id: str | None = None
    strategy: StrategyName | None = None
    shares: float = 0.0
    avg_price: float = 0.0
    realized_pnl_usd: float = 0.0
    opened_at: float = Field(default_factory=time.time)

    @property
    def cost_basis_usd(self) -> float:
        return self.shares * self.avg_price

    def unrealized_pnl_usd(self, mark_price: float) -> float:
        return (mark_price - self.avg_price) * self.shares

    def apply_fill(self, fill: Fill) -> None:
        """Update position with a fill, booking realized PnL on reductions."""
        signed = fill.size_shares if fill.side is Side.BUY else -fill.size_shares
        new_shares = self.shares + signed
        opening_or_increasing = self.shares == 0 or (self.shares > 0) == (signed > 0)

        if opening_or_increasing:
            # weighted average cost of same-direction adds
            if new_shares != 0:
                self.avg_price = (
                    abs(self.shares) * self.avg_price + abs(signed) * fill.price
                ) / abs(new_shares)
        else:
            # reducing / closing -> realize PnL on the closed quantity
            closed = min(abs(self.shares), abs(signed))
            direction = 1 if self.shares > 0 else -1
            self.realized_pnl_usd += direction * (fill.price - self.avg_price) * closed
            if new_shares == 0:
                self.avg_price = 0.0
            elif (new_shares > 0) != (self.shares > 0):
                # flipped through zero -> remainder takes the fill price as basis
                self.avg_price = fill.price

        self.realized_pnl_usd -= fill.fee_usd
        self.shares = new_shares


# --------------------------------------------------------------------------
# Risk decision
# --------------------------------------------------------------------------
class RiskDecision(BaseModel):
    allowed: bool
    reason: str = "ok"
    approved_size_usd: float | None = None
    checks: dict[str, bool] = Field(default_factory=dict)


Signal.model_rebuild()
