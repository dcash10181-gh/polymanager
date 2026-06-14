"""Strategy 1: Board-behind-the-move momentum scalper.

Enters when external reality has moved faster than Polymarket odds. The
external move is supplied as a ``fair_price`` (or ``move_score``) input from a
real data feed (crypto threshold, live sports, macro release, etc.). The
strategy checks that a tradable edge survives spread/depth/staleness, then emits
a short-horizon scalp with a take-profit, stop, and time-stop.
"""

from __future__ import annotations

from app.config import StrategyName
from app.models import Side, Signal, Urgency
from app.strategies.base import Strategy, StrategyContext


class MomentumLagStrategy(Strategy):
    name = StrategyName.momentum_lag

    def _fair_price(self, ctx: StrategyContext, token_id: str, mid: float) -> float | None:
        fair = ctx.ext("fair_price", token_id)
        if fair is not None:
            return float(fair)
        move = ctx.ext("move_score", token_id)
        if move is None:
            return None
        # Translate a [-1, 1] move score into an expected reprice around mid.
        move = max(-1.0, min(1.0, float(move)))
        if abs(move) < self.s.mom_min_external_move_score:
            return None
        # Scale the reprice by remaining room toward the relevant boundary.
        room = (1.0 - mid) if move > 0 else mid
        return max(0.0, min(1.0, mid + move * room * 0.5))

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        s = self.s
        book = ctx.book
        if not book.has_two_sides():
            return []
        token_id = book.token_id
        bid, ask, mid = book.best_bid, book.best_ask, book.midpoint
        if bid is None or ask is None or mid is None:
            return []
        spread = book.spread or 0.0
        if spread > s.mom_max_spread:
            return []

        fair = self._fair_price(ctx, token_id, mid)
        if fair is None:
            return []

        signals: list[Signal] = []
        # Bullish: fair above the ask -> buy.
        if fair - ask >= s.mom_min_edge:
            if book.ask_depth_usd(within=spread) >= s.mom_min_depth_usd:
                edge = fair - ask
                signals.append(self._mk(ctx, token_id, Side.BUY, fair, bid, ask, edge))
        # Bearish: fair below the bid -> sell (requires inventory; risk mgr gates).
        elif bid - fair >= s.mom_min_edge:
            if book.bid_depth_usd(within=spread) >= s.mom_min_depth_usd:
                edge = bid - fair
                signals.append(self._mk(ctx, token_id, Side.SELL, fair, bid, ask, edge))
        return signals

    def _mk(self, ctx, token_id, side, fair, bid, ask, edge) -> Signal:
        s = self.s
        confidence = min(0.95, max(0.1, edge / max(1e-6, s.mom_take_profit)))
        if side is Side.BUY:
            entry = ask
            tp = min(0.999, entry + s.mom_take_profit)
            stop = max(0.001, entry - s.mom_stop_loss)
            max_price = min(0.999, ask + s.mom_max_spread)
        else:
            entry = bid
            tp = max(0.001, entry - s.mom_take_profit)
            stop = min(0.999, entry + s.mom_stop_loss)
            max_price = max(0.001, bid - s.mom_max_spread)
        return Signal(
            market_id=ctx.market.id,
            token_id=token_id,
            strategy=self.name,
            side=side,
            estimated_fair_price=fair,
            best_bid=bid,
            best_ask=ask,
            edge=edge,
            confidence=confidence,
            urgency=Urgency.high,
            max_price=max_price,
            suggested_size_usd=s.strategy_max_usd(StrategyName.momentum_lag),
            take_profit_price=tp,
            stop_price=stop,
            time_stop_seconds=s.mom_time_stop_seconds,
            rationale=f"momentum: fair {fair:.3f} vs {side.value} edge {edge:.3f}",
        )
