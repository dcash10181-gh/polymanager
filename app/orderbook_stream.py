"""Real-time market data manager.

Maintains local per-token :class:`MarketState` (book + rolling price history) so
the signal engine can read best bid/ask, depth, imbalance, velocities, and
staleness without re-fetching. REST polling is the always-available primary;
a WebSocket feed can push updates into the same state via :meth:`on_book`.

If a book is older than ``max_order_book_age_seconds`` it is considered STALE
and the engine must not trade it.
"""

from __future__ import annotations

import time

from app.config import Settings
from app.logger import get_logger
from app.models import MarketState, OrderBook
from app.polymarket_client import PolymarketGateway

log = get_logger("marketdata")


class MarketDataManager:
    def __init__(self, gateway: PolymarketGateway, settings: Settings) -> None:
        self.gateway = gateway
        self.s = settings
        self.states: dict[str, MarketState] = {}

    def state(self, token_id: str) -> MarketState:
        if token_id not in self.states:
            self.states[token_id] = MarketState(token_id=token_id)
        return self.states[token_id]

    def on_book(self, book: OrderBook) -> None:
        """Push a fresh book into state (used by WS feed or poller)."""
        self.state(book.token_id).record(book)

    def get_book(self, token_id: str) -> OrderBook | None:
        return self.state(token_id).book

    def poll(self, token_ids: list[str]) -> dict[str, OrderBook]:
        """REST-poll books for the given tokens and update state."""
        books = self.gateway.get_orderbooks(token_ids)
        for book in books.values():
            self.on_book(book)
        return books

    def is_stale(self, token_id: str, now: float | None = None) -> bool:
        book = self.get_book(token_id)
        if book is None:
            return True
        return book.is_stale(self.s.max_order_book_age_seconds, now)

    def velocities(self, token_id: str, now: float | None = None) -> dict[str, float | None]:
        st = self.state(token_id)
        return {
            "v1s": st.velocity(1, now),
            "v5s": st.velocity(5, now),
            "v15s": st.velocity(15, now),
            "v60s": st.velocity(60, now),
        }
