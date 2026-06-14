"""Strategy 4: Arbitrage and near-arbitrage.

The one strategy whose edge is derived purely from the order books — no
external model required.

  * Binary complement: ``YES_ask + NO_ask < 1`` -> buy both, payout is exactly 1.
  * Multi-outcome basket: ``sum(best_asks) < 1`` for a complete, mutually
    exclusive set -> buy all legs.

Edge only counts if every leg has sufficient depth and the net edge survives
fees and a slippage buffer. Legging risk is real: the executor must place all
legs (ideally FOK/FAK) and the risk manager sizes against the thinnest leg.
"""

from __future__ import annotations

from app.config import StrategyName
from app.models import OrderBook, Side, Signal, SignalLeg, Urgency
from app.strategies.base import Strategy, StrategyContext


class ArbitrageStrategy(Strategy):
    name = StrategyName.arbitrage

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        s = self.s
        books = ctx.books or {ctx.book.token_id: ctx.book}
        # Need a complete set of outcome books to assert a basket.
        token_ids = ctx.market.token_ids
        if len(token_ids) < 2:
            return []
        leg_books: list[OrderBook] = []
        for tid in token_ids:
            b = books.get(tid)
            if b is None or b.best_ask is None:
                return []  # incomplete book set -> cannot assert arbitrage
            leg_books.append(b)

        sum_asks = sum(b.best_ask for b in leg_books)  # type: ignore[misc]
        fee = (s.fee_bps / 10_000.0) * len(leg_books)
        net_edge = 1.0 - sum_asks - fee
        if net_edge < max(s.arb_min_edge, 0.0):
            return []

        # Size against the thinnest leg's available depth at the touch and the
        # arbitrage position cap. One "basket" = buying 1 share of each leg.
        min_depth_usd = min(b.ask_depth_usd(within=0.0) for b in leg_books)
        if min_depth_usd < s.arb_min_depth_usd:
            return []
        # shares per leg bounded by thinnest leg depth (USD/price) and the cap
        thinnest = min(leg_books, key=lambda b: b.ask_depth_usd())
        max_shares_depth = thinnest.ask_depth_usd() / max(1e-6, thinnest.best_ask)
        cap_usd = s.strategy_max_usd(StrategyName.arbitrage)
        basket_cost = sum_asks  # cost to buy one share of every leg
        max_shares_cap = cap_usd / max(1e-6, basket_cost)
        shares = max(0.0, min(max_shares_depth, max_shares_cap))
        if shares <= 0:
            return []

        legs = [
            SignalLeg(token_id=b.token_id, side=Side.BUY, price=b.best_ask,  # type: ignore[arg-type]
                      size_usd=round(b.best_ask * shares, 4))
            for b in leg_books
        ]
        total_usd = round(sum(leg.size_usd for leg in legs), 4)
        return [Signal(
            market_id=ctx.market.id,
            token_id=legs[0].token_id,
            strategy=self.name,
            side=Side.BUY,
            estimated_fair_price=1.0,
            best_bid=None,
            best_ask=sum_asks,
            edge=net_edge,
            confidence=0.9,
            urgency=Urgency.high,
            max_price=min(0.999, leg_books[0].best_ask or 0.999),
            suggested_size_usd=total_usd,
            legs=legs,
            rationale=(f"basket arb: sum_asks={sum_asks:.4f} "
                       f"net_edge={net_edge:.4f} over {len(legs)} legs"),
        )]
