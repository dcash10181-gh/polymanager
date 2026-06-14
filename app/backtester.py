"""Backtester — replay recorded market frames through the live pipeline.

Feeds historical book snapshots through the *same* signal engine, risk manager,
and paper broker used live, so a backtest exercises the real decision code (not
a separate idealized model). Conservative fills come from the PaperBroker.

A "frame" is a dict::

    {
      "timestamp": float,
      "market": Market,
      "books": {token_id: OrderBook},
      "external": {...},   # optional operator inputs (fair_price, true_prob, ...)
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings, StrategyName
from app.logger import get_logger
from app.models import ClaudeReview, RiskLevel
from app.orderbook_stream import MarketDataManager
from app.paper_broker import PaperBroker
from app.portfolio import Portfolio
from app.risk_manager import RiskManager
from app.signal_engine import SignalEngine

log = get_logger("backtest")


def _auto_allow_review() -> ClaudeReview:
    """Backtests bypass the LLM; ambiguity must be pre-filtered in the data."""
    return ClaudeReview(allow_trade=True, risk_level=RiskLevel.low,
                        resolution_ambiguity=False, valid_json=True,
                        comment="backtest auto-allow")


@dataclass
class BacktestResult:
    frames: int = 0
    signals: int = 0
    orders: int = 0
    fills: int = 0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    by_strategy: dict[str, int] = field(default_factory=dict)

    @property
    def total_pnl_usd(self) -> float:
        return self.realized_pnl_usd + self.unrealized_pnl_usd

    def summary(self) -> str:
        return (f"frames={self.frames} signals={self.signals} orders={self.orders} "
                f"fills={self.fills} realized=${self.realized_pnl_usd:.2f} "
                f"unrealized=${self.unrealized_pnl_usd:.2f} "
                f"total=${self.total_pnl_usd:.2f} by_strategy={self.by_strategy}")


class _NullGateway:
    def get_orderbooks(self, ids):  # backtest feeds books directly
        return {}


class Backtester:
    def __init__(self, settings: Settings,
                 enabled: list[StrategyName] | None = None,
                 fill_ratio: float = 0.5) -> None:
        self.s = settings
        self.portfolio = Portfolio(settings)
        self.market_data = MarketDataManager(_NullGateway(), settings)
        self.engine = SignalEngine(settings, self.market_data, self.portfolio, enabled)
        self.risk = RiskManager(settings, self.portfolio)
        self.broker = PaperBroker(settings, self.portfolio, fill_ratio=fill_ratio)
        from app.execution_engine import TradeIntentBuilder
        self.builder = TradeIntentBuilder()

    def run(self, frames) -> BacktestResult:
        res = BacktestResult()
        for frame in frames:
            res.frames += 1
            now = frame.get("timestamp")
            market = frame["market"]
            books = frame.get("books", {})
            for book in books.values():
                self.market_data.on_book(book)
                # First, let resting orders fill against the new book.
                fills = self.broker.process_book(book)
                res.fills += len(fills)
                self.portfolio.set_mark(book.token_id, book.midpoint or
                                        self.portfolio.marks.get(book.token_id, 0.0))

            signals = self.engine.evaluate(market, frame.get("external", {}), now=now)
            for sig in signals:
                # strategies already enforce their own edge; only re-check spread
                ok, _ = sig.passes_basic_filters(0.0, self.s.max_spread)
                if not ok:
                    continue
                res.signals += 1
                res.by_strategy[sig.strategy.value] = \
                    res.by_strategy.get(sig.strategy.value, 0) + 1
                review = _auto_allow_review()
                intents = self.builder.build(sig, sig.suggested_size_usd)
                for intent in intents:
                    decision = self.risk.validate(
                        intent, sig, books.get(intent.token_id),
                        review, self.broker_open_count(), now=now)
                    if not decision.allowed:
                        continue
                    res.orders += 1
                    book = books.get(intent.token_id)
                    fills = []
                    order = self.broker.place_order(intent, book)
                    # immediate fills counted via order state
            # mark unrealized at end of each frame
        res.realized_pnl_usd = round(self.portfolio.realized_pnl_usd, 4)
        res.unrealized_pnl_usd = round(self.portfolio.unrealized_pnl_usd(), 4)
        return res

    def broker_open_count(self) -> int:
        return len(self.broker.open_orders)
