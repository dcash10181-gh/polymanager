"""Strategy 3: Cheap-tail 1-5c convexity buyer.

Buys very cheap outcomes when a credible, near-term catalyst can cause fast
repricing. Most expire worthless, so sizing is tiny and the book-level tail
exposure is capped. The goal is to sell into repricing, not hold to resolution.

Requires an external estimated probability and a credible catalyst flag; absent
them the strategy stays dormant.
"""

from __future__ import annotations

from app.config import StrategyName
from app.models import Side, Signal, Urgency
from app.strategies.base import Strategy, StrategyContext


class CheapTailStrategy(Strategy):
    name = StrategyName.cheap_tail

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        s = self.s
        signals: list[Signal] = []
        for token in ctx.market.tokens:
            book = ctx.book
            if book.token_id != token.token_id:
                continue
            ask = book.best_ask
            bid = book.best_bid
            if ask is None:
                continue
            if not (s.tail_enter_price_min <= ask <= s.tail_enter_price_max):
                continue

            est_prob = ctx.ext("true_prob", token.token_id)
            catalyst = ctx.ext("catalyst", token.token_id, default=False)
            if est_prob is None or not catalyst:
                continue
            est_prob = float(est_prob)

            if est_prob < ask * s.tail_prob_multiple:
                continue
            if book.ask_depth_usd(within=0.01) < s.tail_min_depth_usd:
                continue

            edge = est_prob - ask
            confidence = min(0.6, max(0.05, edge / max(1e-6, ask)))
            tp = min(0.5, ask * s.tail_take_profit_multiple)
            stop = max(0.001, ask * s.tail_stop_loss_fraction)
            signals.append(Signal(
                market_id=ctx.market.id,
                token_id=token.token_id,
                strategy=self.name,
                side=Side.BUY,
                estimated_fair_price=est_prob,
                best_bid=bid,
                best_ask=ask,
                edge=edge,
                confidence=confidence,
                urgency=Urgency.medium,
                max_price=min(s.tail_enter_price_max, ask),
                suggested_size_usd=s.strategy_max_usd(StrategyName.cheap_tail),
                take_profit_price=tp,
                stop_price=stop,
                time_stop_seconds=None,
                rationale=(f"cheap-tail: est_prob {est_prob:.3f} >= "
                           f"ask {ask:.3f} x {s.tail_prob_multiple} w/ catalyst"),
            ))
        return signals
