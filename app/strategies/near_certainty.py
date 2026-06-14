"""Strategy 2: Near-certainty 98-99.9c edge harvester.

Buys outcomes that appear effectively decided but still trade below payout.
Dangerous if oversized — a 99c share risks 99c to make 1c — so size is tiny and
an estimated true probability must clear ``ask + risk_buffer`` before any fire.

The true-probability estimate is an *external input* (an operator model or
Claude's "effectively decided" judgment). Absent it, the strategy stays dormant.
"""

from __future__ import annotations

from app.config import StrategyName
from app.models import Side, Signal, Urgency
from app.strategies.base import Strategy, StrategyContext


class NearCertaintyStrategy(Strategy):
    name = StrategyName.near_certainty

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        s = self.s
        for token in ctx.market.tokens:
            book = ctx.book
            if book.token_id != token.token_id:
                continue
            ask = book.best_ask
            bid = book.best_bid
            if ask is None or bid is None:
                continue
            if not (s.nc_enter_price_min <= ask <= s.nc_enter_price_max):
                continue

            true_prob = ctx.ext("true_prob", token.token_id)
            if true_prob is None:
                # No basis to claim the outcome is decided -> do not trade.
                continue
            true_prob = float(true_prob)

            required = ask + s.nc_risk_buffer
            if true_prob < required:
                continue

            spread = book.spread or 0.0
            if spread > s.max_spread:
                continue
            if book.ask_depth_usd(within=0.005) < s.nc_min_depth_usd:
                continue

            edge = true_prob - ask  # ~ EV per share for a YES bought at ask
            confidence = min(0.99, max(0.0, edge / max(1e-6, 1.0 - ask)))
            size = round(s.strategy_max_usd(StrategyName.near_certainty) *
                         (0.5 + 0.5 * confidence), 4)

            signals.append(Signal(
                market_id=ctx.market.id,
                token_id=token.token_id,
                strategy=self.name,
                side=Side.BUY,
                estimated_fair_price=min(1.0, true_prob),
                best_bid=bid,
                best_ask=ask,
                edge=edge,
                confidence=confidence,
                urgency=Urgency.low,
                max_price=min(s.nc_enter_price_max, ask),
                suggested_size_usd=size,
                take_profit_price=s.nc_take_profit,
                stop_price=s.nc_stop_loss,
                time_stop_seconds=None,  # often held to resolution if risk allows
                rationale=(f"near-certainty: est_prob={true_prob:.3f} >= "
                           f"ask {ask:.3f}+buf {s.nc_risk_buffer:.3f}"),
            ))
        return signals
