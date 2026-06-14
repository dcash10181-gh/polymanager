"""Strategy base class and shared context."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict

from app.config import Settings, StrategyName
from app.models import Market, MarketState, OrderBook, Position, Signal


class StrategyContext(BaseModel):
    """Everything a strategy needs to evaluate one market at one instant.

    ``external`` is the operator-supplied alpha channel. It may contain, keyed
    by token id or globally:
      * ``"true_prob:<token_id>"`` / ``"true_prob"`` — estimated resolution prob
      * ``"fair_price:<token_id>"``                  — estimated fair price
      * ``"move_score:<token_id>"``                  — external move score [-1,1]
      * ``"catalyst:<token_id>"``                    — bool, a credible catalyst
    Strategies that need external inputs stay dormant when they are absent, so
    the bot never fabricates edge.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    market: Market
    book: OrderBook
    books: dict[str, OrderBook] = {}   # all tokens of the market (for arbitrage)
    state: MarketState | None = None
    position: Position | None = None
    external: dict = {}
    now: float | None = None

    def ext(self, key: str, token_id: str | None = None, default=None):
        if token_id is not None:
            scoped = self.external.get(f"{key}:{token_id}")
            if scoped is not None:
                return scoped
        return self.external.get(key, default)


class Strategy(ABC):
    name: StrategyName

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        ...
