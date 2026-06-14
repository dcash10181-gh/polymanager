"""Paper broker — conservative limit-order fill simulation.

Models the parts of reality that destroy naive backtests: limited resting
depth, partial fills, queue position, slippage from walking the book, and the
fact that *touching* a price does not guarantee a fill.

Fill rules (Section 18 of the plan):
  * A BUY limit fills only against asks priced <= the limit.
  * A SELL limit fills only against bids priced >= the limit.
  * Only a fraction (``fill_ratio``) of resting size is assumed available to
    us, simulating queue priority — we are never first in line.
"""

from __future__ import annotations

import time

from app.config import Settings, StrategyName
from app.logger import get_logger
from app.models import Fill, Order, OrderBook, OrderIntent, OrderStatus, Side
from app.portfolio import Portfolio

log = get_logger("paper")


class PaperBroker:
    def __init__(self, settings: Settings, portfolio: Portfolio,
                 fill_ratio: float = 0.5, db=None) -> None:
        self.s = settings
        self.portfolio = portfolio
        self.fill_ratio = fill_ratio
        self.db = db
        self.open_orders: dict[str, Order] = {}
        self._strategy_by_order: dict[str, StrategyName] = {}
        self._market_by_order: dict[str, str | None] = {}

    # -- order lifecycle ----------------------------------------------------
    def place_order(self, intent: OrderIntent, book: OrderBook | None = None) -> Order:
        order = Order(
            client_order_id=intent.client_order_id,
            token_id=intent.token_id,
            side=intent.side,
            price=intent.price,
            size_shares=intent.size_shares,
            strategy=intent.strategy,
            status=OrderStatus.open,
        )
        self.open_orders[order.client_order_id] = order
        self._strategy_by_order[order.client_order_id] = intent.strategy
        self._market_by_order[order.client_order_id] = intent.market_id
        if self.db:
            self.db.save_order(order, is_paper=True)
        # Attempt an immediate (marketable) fill against the current book.
        if book is not None:
            self._try_fill(order, book)
        return order

    def cancel(self, client_order_id: str) -> bool:
        order = self.open_orders.pop(client_order_id, None)
        if not order:
            return False
        order.status = OrderStatus.canceled
        order.updated_at = time.time()
        if self.db:
            self.db.save_order(order, is_paper=True)
        return True

    def cancel_all(self) -> int:
        ids = list(self.open_orders.keys())
        for cid in ids:
            self.cancel(cid)
        return len(ids)

    def cancel_stale_orders(self, max_age_seconds: float | None = None,
                            now: float | None = None) -> int:
        max_age = (self.s.stale_order_cancel_seconds
                   if max_age_seconds is None else max_age_seconds)
        now = now if now is not None else time.time()
        stale = [cid for cid, o in self.open_orders.items()
                 if now - o.created_at > max_age]
        for cid in stale:
            self.cancel(cid)
        return len(stale)

    # -- fill engine --------------------------------------------------------
    def process_book(self, book: OrderBook) -> list[Fill]:
        """Re-attempt fills for all resting orders on this token."""
        fills: list[Fill] = []
        for cid in list(self.open_orders.keys()):
            order = self.open_orders.get(cid)
            if order and order.token_id == book.token_id:
                fills.extend(self._try_fill(order, book))
        return fills

    def _try_fill(self, order: Order, book: OrderBook) -> list[Fill]:
        remaining = order.remaining_shares
        if remaining <= 0:
            return []
        filled_shares, avg_price = self._simulate(order.side, order.price, remaining, book)
        if filled_shares <= 0:
            return []

        fee = avg_price * filled_shares * (self.s.fee_bps / 10_000.0)
        fill = Fill(
            client_order_id=order.client_order_id,
            token_id=order.token_id,
            side=order.side,
            price=avg_price,
            size_shares=round(filled_shares, 4),
            fee_usd=round(fee, 6),
            is_paper=True,
        )
        order.filled_shares += filled_shares
        order.updated_at = time.time()
        if order.remaining_shares <= 1e-9:
            order.status = OrderStatus.filled
            self.open_orders.pop(order.client_order_id, None)
        else:
            order.status = OrderStatus.partially_filled

        self.portfolio.apply_fill(
            fill,
            market_id=self._market_by_order.get(order.client_order_id),
            strategy=self._strategy_by_order.get(order.client_order_id),
        )
        if self.db:
            self.db.save_fill(fill)
            self.db.save_order(order, is_paper=True)
            self.db.save_position(self.portfolio.get_position(order.token_id))
        log.debug("paper fill %s %s %.2f@%.4f", order.side.value, order.token_id,
                  filled_shares, avg_price)
        return [fill]

    def _simulate(self, side: Side, limit: float, remaining: float,
                  book: OrderBook) -> tuple[float, float]:
        """Walk the book and return (filled_shares, volume-weighted avg price)."""
        if side is Side.BUY:
            levels = sorted((lvl for lvl in book.asks if lvl.price <= limit + 1e-12),
                            key=lambda l: l.price)
        else:
            levels = sorted((lvl for lvl in book.bids if lvl.price >= limit - 1e-12),
                            key=lambda l: l.price, reverse=True)
        if not levels:
            return 0.0, 0.0

        to_fill = remaining
        filled = 0.0
        notional = 0.0
        for lvl in levels:
            available = lvl.size * self.fill_ratio
            take = min(to_fill, available)
            if take <= 0:
                continue
            filled += take
            notional += take * lvl.price
            to_fill -= take
            if to_fill <= 1e-9:
                break
        if filled <= 0:
            return 0.0, 0.0
        return filled, notional / filled
