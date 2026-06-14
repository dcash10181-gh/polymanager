"""Main loop — wires the whole pipeline together and runs it.

Default mode is ``paper``. Live order placement only happens when ``BOT_MODE``
is ``tiny_live``/``live`` *and* credentials are configured. Any unhandled
anomaly trips the kill switch and halts new entries.

Run::

    python -m app.main            # uses BOT_MODE from .env (default: paper)
    python -m app.main --once     # one pass over the watchlist, then exit
    python -m app.main --iterations 10
"""

from __future__ import annotations

import argparse
import logging
import signal
import time

from app.claude_reasoner import ClaudeReasoner
from app.config import BotMode, Settings, get_settings
from app.db import Database
from app.execution_engine import ExecutionEngine
from app.exit_manager import ExitManager
from app.logger import get_logger, log_event, setup_logging
from app.market_discovery import MarketDiscovery
from app.models import Market
from app.orderbook_stream import MarketDataManager
from app.polymarket_client import PolymarketGateway
from app.portfolio import Portfolio
from app.risk_manager import KillSwitch, RiskManager
from app.signal_engine import SignalEngine

log = get_logger("main")


class TradingApp:
    def __init__(self, settings: Settings | None = None,
                 gateway: PolymarketGateway | None = None) -> None:
        self.s = settings or get_settings()
        self.db = Database(self.s.db_path)
        self.gateway = gateway or PolymarketGateway(self.s)
        self.portfolio = Portfolio(self.s)
        self.market_data = MarketDataManager(self.gateway, self.s)
        self.discovery = MarketDiscovery(self.gateway, self.s)
        self.engine = SignalEngine(self.s, self.market_data, self.portfolio)
        self.reasoner = ClaudeReasoner(self.s)
        self.kill = KillSwitch()
        self.risk = RiskManager(self.s, self.portfolio, self.kill, db=self.db)
        self.execution = ExecutionEngine.create(self.s, self.portfolio, db=self.db)
        self.exits = ExitManager(self.s)
        self._stop = False
        # Operator alpha: callable(market) -> external-signal dict. Wired from
        # config when enabled; can also be set directly (tests, custom feeds).
        self.external_provider = self._build_external_provider()

    # ----------------------------------------------------------------------
    def _build_external_provider(self):
        providers = []
        if self.s.enable_crypto_alpha:
            from app.alpha.crypto_threshold import CryptoThresholdProvider
            from app.alpha.price_feed import PriceFeed
            feed = PriceFeed(base_url=self.s.alpha_price_host)
            providers.append(CryptoThresholdProvider(
                self.s, feed, catalyst_horizon_days=self.s.alpha_catalyst_horizon_days))
            log.warning("crypto-threshold alpha provider enabled")
        if not providers:
            return None
        from app.alpha.base import CompositeProvider
        return CompositeProvider(providers)

    def external_for(self, market: Market) -> dict:
        if self.external_provider is None:
            return {}
        try:
            return self.external_provider(market) or {}
        except Exception:
            log.exception("external signal provider failed")
            return {}

    def process_market(self, market: Market) -> None:
        token_ids = market.token_ids
        if not token_ids:
            return
        t0 = time.time()
        try:
            books = self.market_data.poll(token_ids)
        except Exception as e:
            log.warning("book poll failed for %s: %s", market.id, e)
            return
        latency_ms = (time.time() - t0) * 1000.0

        # Re-fill resting paper orders, mark positions, and manage exits first.
        for book in books.values():
            if self.market_data.is_stale(book.token_id):
                self.execution.cancel_stale_orders()
                continue
            self.execution.process_fills(book)
            self.portfolio.set_mark(book.token_id, book.midpoint or
                                    self.portfolio.marks.get(book.token_id, 0.0))
            self._manage_exit(book)

        signals = self.engine.evaluate(market, self.external_for(market))
        for sig in signals:
            self.db.save_signal(sig)
            ok, reason = sig.passes_basic_filters(self.s.mom_min_edge, self.s.max_spread)
            if not ok:
                self.db.save_rejection("basic_filters", reason,
                                      {"token_id": sig.token_id})
                continue

            book = self.market_data.get_book(sig.token_id)
            position = self.portfolio.get_state(sig.token_id)
            review = self.reasoner.review(market, sig, book, position)
            self.db.save_review(market.id, sig.token_id, review)

            # Build an intent to validate (size trimmed by the risk manager).
            from app.execution_engine import TradeIntentBuilder
            intents = TradeIntentBuilder.build(sig, sig.suggested_size_usd)
            primary = intents[0]
            decision = self.risk.validate(
                primary, sig, book, review,
                open_orders_count=self.execution.open_orders_count())
            if not decision.allowed:
                continue
            placed = self.execution.submit(sig, decision.approved_size_usd, book)
            for intent in placed:
                self.exits.register(intent)

        self.risk.check_kill_switches(latency_ms=latency_ms)

    def _manage_exit(self, book) -> None:
        """Flatten a position if it has hit its take-profit / stop / time-stop."""
        position = self.portfolio.get_state(book.token_id)
        if position is None or abs(position.shares) < 1e-9:
            return
        exit_intent = self.exits.evaluate(position, book)
        if exit_intent is not None:
            self.execution.place_exit(exit_intent, book)
            self.execution.process_fills(book)  # let the exit fill immediately (paper)

    def run_once(self) -> None:
        try:
            markets = self.discovery.get_watchlist()
        except Exception as e:
            log.warning("watchlist refresh failed (network?): %s", e)
            return
        log_event(log, logging.INFO, f"scanning {len(markets)} markets "
                  f"[mode={self.s.bot_mode.value}]", markets=len(markets))
        for market in markets:
            if self._stop or self.kill.tripped:
                break
            self.db.save_market(market)
            try:
                self.process_market(market)
            except Exception:
                log.exception("error processing market %s — pausing", market.id)
                self.kill.trip(f"exception_in_loop:{market.id}")
                break

        self.execution.cancel_stale_orders()
        self.portfolio.refresh()
        snap = self.portfolio.snapshot()
        self.db.save_pnl_snapshot(snap["realized_pnl_usd"], snap["unrealized_pnl_usd"],
                                  snap["exposure_usd"], snap["open_positions"])
        log_event(log, logging.INFO, f"pnl {snap}", **snap)

    def run(self, iterations: int | None = None) -> None:
        self._install_signal_handlers()
        log.warning("starting in %s mode (kill switch armed)", self.s.bot_mode.value)
        if self.s.bot_mode.places_real_orders:
            log.warning("!!! REAL ORDERS ENABLED — this risks real funds !!!")
        i = 0
        while not self._stop:
            if self.kill.tripped:
                log.error("kill switch tripped (%s) — halting new entries; "
                          "manual reset required", self.kill.reasons)
                break
            self.run_once()
            i += 1
            if iterations is not None and i >= iterations:
                break
            time.sleep(self.s.loop_interval_seconds)
        self.shutdown()

    def shutdown(self) -> None:
        log.warning("shutting down — cancelling open orders")
        try:
            self.execution.cancel_all()
        except Exception:
            log.exception("error during shutdown cancel_all")
        self.db.close()

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):
            log.warning("signal %s received — stopping", signum)
            self._stop = True
        try:
            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGTERM, handler)
        except ValueError:
            pass  # not in main thread (e.g. tests)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymanager trading agent")
    parser.add_argument("--once", action="store_true", help="single pass then exit")
    parser.add_argument("--iterations", type=int, default=None,
                        help="number of loop iterations before exiting")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level)

    if settings.bot_mode is BotMode.live:
        log.warning("BOT_MODE=live. Ensure you have validated paper + shadow + "
                    "tiny_live first. Ctrl-C to abort within 5s.")
        time.sleep(5)

    app = TradingApp(settings)
    app.run(iterations=1 if args.once else args.iterations)


if __name__ == "__main__":
    main()
