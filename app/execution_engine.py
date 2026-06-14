"""Execution engine.

Routes approved trades to the right venue based on ``BOT_MODE``:
  * research / shadow -> log the intent only (no order)
  * paper             -> simulate via the PaperBroker
  * tiny_live / live  -> place real limit orders via the LiveExecutor

The LiveExecutor (real signing + order placement through ``py-clob-client``) is
**only constructed when the mode needs credentials**, so paper/research runs can
never place an order and never import the signing SDK.
"""

from __future__ import annotations

import logging

from app.config import BotMode, Settings, StrategyName
from app.logger import get_logger, log_event
from app.models import (ClaudeReview, OrderBook, OrderIntent, Signal,
                        new_client_order_id)
from app.paper_broker import PaperBroker
from app.portfolio import Portfolio

log = get_logger("execution")


# --------------------------------------------------------------------------
# Build order intents from an approved signal.
# --------------------------------------------------------------------------
class TradeIntentBuilder:
    @staticmethod
    def build(signal: Signal, approved_size_usd: float | None = None) -> list[OrderIntent]:
        """One intent per leg (arbitrage), or a single intent otherwise."""
        if signal.legs:
            # Arbitrage basket: place every leg. Scale legs proportionally if the
            # risk manager trimmed the basket notional.
            total = signal.suggested_size_usd or 1.0
            scale = (approved_size_usd / total) if approved_size_usd else 1.0
            scale = max(0.0, min(1.0, scale))
            return [OrderIntent(
                market_id=signal.market_id,
                token_id=leg.token_id,
                side=leg.side,
                price=leg.price,
                size_usd=round(leg.size_usd * scale, 4),
                strategy=signal.strategy,
                client_order_id=new_client_order_id(),
            ) for leg in signal.legs if leg.size_usd * scale > 0]

        size = approved_size_usd if approved_size_usd is not None else signal.suggested_size_usd
        return [OrderIntent(
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=signal.side,
            price=signal.max_price,            # limit at the worst acceptable price
            size_usd=round(size, 4),
            strategy=signal.strategy,
            take_profit_price=signal.take_profit_price,
            stop_price=signal.stop_price,
            time_stop_seconds=signal.time_stop_seconds,
        )]


# --------------------------------------------------------------------------
# Live executor — gated, real money. Never imported in paper mode.
# --------------------------------------------------------------------------
class LiveExecutor:
    """Authenticated order placement via the official Polymarket CLOB SDK."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        if not settings.polymarket_private_key:
            raise RuntimeError("LiveExecutor requires POLYMARKET_PRIVATE_KEY")
        # Imports are local so paper/research mode never loads the signing SDK.
        from py_clob_client.client import ClobClient

        kwargs = dict(
            host=settings.polymarket_host,
            key=settings.polymarket_private_key,
            chain_id=settings.chain_id,
        )
        if settings.polymarket_funder_address:
            kwargs["funder"] = settings.polymarket_funder_address
            kwargs["signature_type"] = settings.polymarket_signature_type
        self.client = ClobClient(**kwargs)
        # Derive (or create) L2 API credentials for order endpoints.
        creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(creds)
        log.warning("LiveExecutor active — REAL orders enabled (mode=%s)",
                    settings.bot_mode.value)

    def place_limit_order(self, intent: OrderIntent) -> dict:
        from py_clob_client.clob_types import OrderArgs, OrderType

        args = OrderArgs(
            token_id=intent.token_id,
            price=round(intent.price, 4),
            size=intent.size_shares,
            side=intent.side.value,
        )
        signed = self.client.create_order(args)
        resp = self.client.post_order(signed, OrderType.GTC)
        return resp

    def cancel(self, order_id: str) -> dict:
        return self.client.cancel(order_id)

    def cancel_all(self) -> dict:
        return self.client.cancel_all()

    def get_open_orders(self) -> list:
        return self.client.get_orders() or []

    def get_positions(self) -> list:
        # Positions live on the Data API; the gateway handles that read path.
        return []


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------
class ExecutionEngine:
    def __init__(self, settings: Settings, portfolio: Portfolio,
                 paper_broker: PaperBroker | None = None,
                 live_executor: LiveExecutor | None = None, db=None) -> None:
        self.s = settings
        self.portfolio = portfolio
        self.paper = paper_broker
        self.live = live_executor
        self.db = db
        self.builder = TradeIntentBuilder()

    @classmethod
    def create(cls, settings: Settings, portfolio: Portfolio, db=None) -> "ExecutionEngine":
        """Wire up the right backend for the configured mode."""
        paper = None
        live = None
        if settings.bot_mode.is_paper:
            paper = PaperBroker(settings, portfolio, db=db)
        elif settings.bot_mode.places_real_orders:
            live = LiveExecutor(settings)  # raises if creds missing — fail loud
        # research/shadow: neither (intents are logged only)
        return cls(settings, portfolio, paper_broker=paper, live_executor=live, db=db)

    def open_orders_count(self) -> int:
        if self.paper:
            return len(self.paper.open_orders)
        if self.live:
            try:
                return len(self.live.get_open_orders())
            except Exception:
                return 0
        return 0

    def submit(self, signal: Signal, approved_size_usd: float | None,
               book: OrderBook | None = None) -> list[OrderIntent]:
        intents = self.builder.build(signal, approved_size_usd)
        mode = self.s.bot_mode

        for intent in intents:
            if mode in (BotMode.research, BotMode.shadow):
                log_event(log, logging.INFO, f"[{mode.value}] intent (no order)",
                          stage="intent", strategy=intent.strategy.value,
                          token_id=intent.token_id, side=intent.side.value,
                          price=intent.price, size_usd=intent.size_usd)
                if self.db:
                    self.db.save_rejection(mode.value, "shadow_no_execute",
                                          {"token_id": intent.token_id})
            elif mode.is_paper and self.paper:
                self.paper.place_order(intent, book)
            elif mode.places_real_orders and self.live:
                try:
                    resp = self.live.place_limit_order(intent)
                    log.info("live order placed: %s -> %s", intent.client_order_id, resp)
                except Exception:
                    log.exception("live order placement failed")
                    raise
        return intents

    # -- order maintenance --------------------------------------------------
    def cancel_stale_orders(self, now: float | None = None) -> int:
        if self.paper:
            return self.paper.cancel_stale_orders(now=now)
        # Live stale-order cancellation would diff open orders by age here.
        return 0

    def cancel_all(self) -> None:
        if self.paper:
            self.paper.cancel_all()
        if self.live:
            try:
                self.live.cancel_all()
            except Exception:
                log.exception("live cancel_all failed")

    def process_fills(self, book: OrderBook) -> None:
        """Re-evaluate resting paper orders against a fresh book."""
        if self.paper:
            self.paper.process_book(book)
