"""Exit manager — realizes PnL by closing open positions.

Signals carry a take-profit, stop, and time-stop, but nothing acts on them once
a position is open. The exit manager records those targets when an entry is
placed and, on every fresh book, decides whether to flatten the position.

Exits *reduce* risk, so they are allowed even when the kill switch has halted
new entries (the plan's "flatten_positions_if_possible"). They route through
``ExecutionEngine.place_exit`` rather than the entry risk gate.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.config import Settings, StrategyName
from app.logger import get_logger, log_event
from app.models import OrderBook, OrderIntent, Position, Side, new_client_order_id

log = get_logger("exit")


@dataclass
class ExitPlan:
    strategy: StrategyName
    take_profit: float | None
    stop: float | None
    time_stop_seconds: int | None
    registered_at: float


class ExitManager:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.plans: dict[str, ExitPlan] = {}

    # ----------------------------------------------------------------------
    def register(self, intent: OrderIntent) -> None:
        """Record exit targets for an entry intent (skips no-target legs)."""
        if (intent.take_profit_price is None and intent.stop_price is None
                and intent.time_stop_seconds is None):
            return  # e.g. arbitrage legs are held, not scalped
        self.plans[intent.token_id] = ExitPlan(
            strategy=intent.strategy,
            take_profit=intent.take_profit_price,
            stop=intent.stop_price,
            time_stop_seconds=intent.time_stop_seconds,
            registered_at=time.time(),
        )

    def forget(self, token_id: str) -> None:
        self.plans.pop(token_id, None)

    # ----------------------------------------------------------------------
    def evaluate(self, position: Position, book: OrderBook,
                 now: float | None = None) -> OrderIntent | None:
        """Return a flattening intent if an exit condition is met, else None."""
        now = now if now is not None else time.time()
        shares = position.shares
        if abs(shares) < 1e-9:
            self.forget(position.token_id)
            return None
        plan = self.plans.get(position.token_id)
        if plan is None:
            return None
        if book.is_stale(self.s.max_order_book_age_seconds, now):
            return None  # don't act on stale marks
        mark = book.midpoint
        if mark is None:
            return None

        long = shares > 0
        reason = self._exit_reason(plan, long, mark, position.opened_at, now)
        if reason is None:
            return None

        # Flatten with a marketable limit: sell into the bid / buy from the ask.
        if long:
            exit_side, exit_price = Side.SELL, (book.best_bid or mark)
        else:
            exit_side, exit_price = Side.BUY, (book.best_ask or mark)
        size_usd = round(abs(shares) * exit_price, 4)
        if size_usd <= 0:
            return None

        log_event(log, logging.INFO,
                  f"exit {position.token_id} ({reason}) {exit_side.value} "
                  f"{abs(shares):.2f}@{exit_price:.4f}",
                  stage="exit", reason=reason, token_id=position.token_id,
                  side=exit_side.value, price=exit_price)
        return OrderIntent(
            market_id=position.market_id or "",
            token_id=position.token_id,
            side=exit_side,
            price=exit_price,
            size_usd=size_usd,
            strategy=position.strategy or plan.strategy,
            client_order_id=new_client_order_id(),
        )

    @staticmethod
    def _exit_reason(plan: ExitPlan, long: bool, mark: float,
                     opened_at: float, now: float) -> str | None:
        tp, stop = plan.take_profit, plan.stop
        if tp is not None and ((long and mark >= tp) or (not long and mark <= tp)):
            return "take_profit"
        if stop is not None and ((long and mark <= stop) or (not long and mark >= stop)):
            return "stop_loss"
        if plan.time_stop_seconds is not None and (now - opened_at) >= plan.time_stop_seconds:
            return "time_stop"
        return None
