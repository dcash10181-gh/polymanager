"""Portfolio: positions, exposure, and PnL accounting.

The single source of truth for what the bot holds and how much risk is on. Fed
by fills from either the paper broker or the live executor. Provides the
exposure numbers the risk manager enforces against.
"""

from __future__ import annotations

import time

from app.config import Settings, StrategyName
from app.models import Fill, Position, Side


class Portfolio:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.realized_pnl_usd: float = 0.0
        # Daily loss tracking (reset at UTC midnight).
        self._day = self._utc_day()
        self._day_start_realized: float = 0.0
        # Marks for unrealized PnL / exit logic.
        self.marks: dict[str, float] = {}

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _utc_day() -> int:
        return int(time.time() // 86400)

    def _roll_day_if_needed(self) -> None:
        today = self._utc_day()
        if today != self._day:
            self._day = today
            self._day_start_realized = self.realized_pnl_usd

    def get_position(self, token_id: str) -> Position:
        if token_id not in self.positions:
            self.positions[token_id] = Position(token_id=token_id)
        return self.positions[token_id]

    def get_state(self, token_id: str) -> Position | None:
        return self.positions.get(token_id)

    # -- mutation -----------------------------------------------------------
    def apply_fill(self, fill: Fill, market_id: str | None = None,
                   strategy: StrategyName | None = None) -> None:
        self._roll_day_if_needed()
        pos = self.get_position(fill.token_id)
        if market_id:
            pos.market_id = market_id
        if strategy and pos.strategy is None:
            pos.strategy = strategy
        before = pos.realized_pnl_usd
        pos.apply_fill(fill)
        self.realized_pnl_usd += (pos.realized_pnl_usd - before)
        self.fills.append(fill)
        self.marks[fill.token_id] = fill.price
        if abs(pos.shares) < 1e-9:
            pos.shares = 0.0

    def set_mark(self, token_id: str, price: float) -> None:
        self.marks[token_id] = price

    # -- exposure / pnl -----------------------------------------------------
    def market_exposure_usd(self, market_id: str) -> float:
        return sum(abs(p.cost_basis_usd) for p in self.positions.values()
                   if p.market_id == market_id and p.shares != 0)

    def token_exposure_usd(self, token_id: str) -> float:
        p = self.positions.get(token_id)
        return abs(p.cost_basis_usd) if p else 0.0

    def total_exposure_usd(self) -> float:
        return sum(abs(p.cost_basis_usd) for p in self.positions.values() if p.shares != 0)

    def strategy_exposure_usd(self, strategy: StrategyName) -> float:
        return sum(abs(p.cost_basis_usd) for p in self.positions.values()
                   if p.strategy is strategy and p.shares != 0)

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if abs(p.shares) > 1e-9]

    def unrealized_pnl_usd(self) -> float:
        total = 0.0
        for p in self.open_positions():
            mark = self.marks.get(p.token_id, p.avg_price)
            total += p.unrealized_pnl_usd(mark)
        return total

    def total_pnl_usd(self) -> float:
        return self.realized_pnl_usd + self.unrealized_pnl_usd()

    def daily_pnl_usd(self) -> float:
        """Realized PnL since the start of the current UTC day."""
        self._roll_day_if_needed()
        return self.realized_pnl_usd - self._day_start_realized

    def daily_loss_fraction(self) -> float:
        """Positive number = fraction of bankroll lost today (0 if up)."""
        dp = self.daily_pnl_usd()
        if dp >= 0:
            return 0.0
        return abs(dp) / self.s.bankroll_usd

    def refresh(self) -> None:
        """Hook for reconciling against the exchange in live mode (no-op here)."""
        self._roll_day_if_needed()

    def snapshot(self) -> dict:
        return {
            "realized_pnl_usd": round(self.realized_pnl_usd, 4),
            "unrealized_pnl_usd": round(self.unrealized_pnl_usd(), 4),
            "exposure_usd": round(self.total_exposure_usd(), 4),
            "open_positions": len(self.open_positions()),
            "daily_pnl_usd": round(self.daily_pnl_usd(), 4),
        }
