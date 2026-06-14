"""Signal engine — orchestrates the strategies into ranked signals.

Produces structured :class:`Signal` objects; it never places trades. For each
market it builds a :class:`StrategyContext` (book + state + position + external
inputs) and runs every enabled strategy, then ranks the resulting signals by an
opportunity score (Section 23 of the plan).
"""

from __future__ import annotations

import time

from app.config import Settings, StrategyName
from app.logger import get_logger
from app.models import Market, OrderBook, Signal
from app.orderbook_stream import MarketDataManager
from app.portfolio import Portfolio
from app.strategies.arbitrage import ArbitrageStrategy
from app.strategies.base import Strategy, StrategyContext
from app.strategies.cheap_tail import CheapTailStrategy
from app.strategies.momentum_lag import MomentumLagStrategy
from app.strategies.near_certainty import NearCertaintyStrategy

log = get_logger("signals")

_DEFAULT_STRATEGIES = {
    StrategyName.near_certainty: NearCertaintyStrategy,
    StrategyName.momentum_lag: MomentumLagStrategy,
    StrategyName.cheap_tail: CheapTailStrategy,
    StrategyName.arbitrage: ArbitrageStrategy,
}


class SignalEngine:
    def __init__(self, settings: Settings, market_data: MarketDataManager,
                 portfolio: Portfolio,
                 enabled: list[StrategyName] | None = None) -> None:
        self.s = settings
        self.market_data = market_data
        self.portfolio = portfolio
        names = enabled if enabled is not None else list(_DEFAULT_STRATEGIES)
        self.strategies: list[Strategy] = [
            _DEFAULT_STRATEGIES[n](settings) for n in names
        ]

    def evaluate(self, market: Market, external: dict | None = None,
                 now: float | None = None) -> list[Signal]:
        now = now if now is not None else time.time()
        external = external or {}
        # Gather books for all the market's tokens from local state.
        books: dict[str, OrderBook] = {}
        for tid in market.token_ids:
            book = self.market_data.get_book(tid)
            if book is not None:
                books[tid] = book
        if not books:
            return []

        out: list[Signal] = []
        for token_id, book in books.items():
            if self.market_data.is_stale(token_id, now):
                continue  # never trade a stale book
            ctx = StrategyContext(
                market=market,
                book=book,
                books=books,
                state=self.market_data.state(token_id),
                position=self.portfolio.get_state(token_id),
                external=external,
                now=now,
            )
            for strat in self.strategies:
                # Arbitrage is market-level; evaluate it once (on first token).
                if strat.name is StrategyName.arbitrage and token_id != market.token_ids[0]:
                    continue
                try:
                    out.extend(strat.evaluate(ctx))
                except Exception:  # one bad strategy must not kill the loop
                    log.exception("strategy %s failed on %s", strat.name, market.id)
        return self.rank(out)

    def rank(self, signals: list[Signal]) -> list[Signal]:
        return sorted(signals, key=self.opportunity_score, reverse=True)

    def opportunity_score(self, sig: Signal) -> float:
        """Section 23 ranking: reward EV/liquidity/fill, penalize ambiguity."""
        ev_per_dollar = sig.edge  # edge already ~ EV per share per dollar
        # crude EV/minute proxy: faster strategies (shorter time stop) score more
        horizon_min = (sig.time_stop_seconds or 600) / 60.0
        ev_per_minute = ev_per_dollar / max(0.5, horizon_min)
        liquidity_score = min(1.0, sig.suggested_size_usd / max(1.0, self.s.usd(0.01)))
        fill_prob = sig.confidence
        ambiguity = len(sig.risk_flags) * 0.1
        return (ev_per_dollar * 0.35
                + ev_per_minute * 0.25
                + liquidity_score * 0.15
                + fill_prob * 0.10
                - ambiguity * 0.10)
