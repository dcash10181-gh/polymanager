"""Deterministic risk manager — the final authority.

Overrides both the signal engine and Claude. Every order intent must clear a
fixed set of hard checks before it can be placed. Also owns the kill switch and
the automatic triggers that pause trading on anomalies.

Nothing here calls an LLM or an exchange; it is pure, synchronous, and the most
heavily tested part of the system.
"""

from __future__ import annotations

import logging

from app.config import BotMode, Settings, StrategyName
from app.logger import get_logger, log_event
from app.models import ClaudeReview, OrderBook, OrderIntent, RiskDecision, Side, Signal
from app.portfolio import Portfolio

log = get_logger("risk")


class KillSwitch:
    """Global trading halt. When tripped, no new entries are allowed."""

    def __init__(self) -> None:
        self.tripped = False
        self.reasons: list[str] = []

    def trip(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        if not self.tripped:
            self.tripped = True
            log.error("KILL SWITCH TRIPPED: %s", reason)

    def reset(self) -> None:
        self.tripped = False
        self.reasons.clear()
        log.warning("kill switch manually reset")

    @property
    def new_entries_allowed(self) -> bool:
        return not self.tripped


class RiskManager:
    def __init__(self, settings: Settings, portfolio: Portfolio,
                 kill_switch: KillSwitch | None = None, db=None) -> None:
        self.s = settings
        self.portfolio = portfolio
        self.kill = kill_switch or KillSwitch()
        self.db = db
        self._rejections = 0
        self._submissions = 0

    # ----------------------------------------------------------------------
    def validate(self, intent: OrderIntent, signal: Signal,
                 book: OrderBook | None, review: ClaudeReview,
                 open_orders_count: int = 0,
                 now: float | None = None) -> RiskDecision:
        s = self.s
        checks: dict[str, bool] = {}

        def fail(reason: str) -> RiskDecision:
            checks_failed = reason
            self._record_rejection(intent, checks_failed)
            return RiskDecision(allowed=False, reason=checks_failed, checks=checks)

        # --- global state ---------------------------------------------------
        checks["kill_switch_ok"] = self.kill.new_entries_allowed
        if not checks["kill_switch_ok"]:
            return fail(f"kill_switch_tripped: {','.join(self.kill.reasons)}")

        checks["mode_ok"] = s.bot_mode is not BotMode.paused
        if not checks["mode_ok"]:
            return fail("bot_paused")

        # --- Claude review gate --------------------------------------------
        checks["claude_valid_json"] = review.valid_json
        if not review.valid_json:
            return fail("claude_invalid_json")
        checks["claude_allow_trade"] = review.allow_trade
        if not review.allow_trade:
            return fail(f"claude_blocked: {review.comment}")
        checks["resolution_clear"] = not review.resolution_ambiguity
        if review.resolution_ambiguity:
            return fail("resolution_ambiguity")

        # In any order-placing mode the LLM reviewer must be live (not the
        # offline auto-allow), otherwise there is no ambiguity check at all.
        if s.bot_mode.places_real_orders and s.reasoner_disabled:
            return fail("reasoner_disabled_in_live_mode")

        # --- price sanity ---------------------------------------------------
        checks["price_in_range"] = 0 < intent.price < 1
        if not checks["price_in_range"]:
            return fail("price_out_of_range")
        # don't chase: buys must not pay above the touch + max_spread, etc.
        if book is not None and book.best_ask is not None and intent.side is Side.BUY:
            if intent.price > book.best_ask + s.max_spread + 1e-9:
                return fail("buy_price_chasing")
        if book is not None and book.best_bid is not None and intent.side is Side.SELL:
            if intent.price < book.best_bid - s.max_spread - 1e-9:
                return fail("sell_price_chasing")

        # --- data freshness / spread ---------------------------------------
        if book is not None:
            checks["book_fresh"] = not book.is_stale(s.max_order_book_age_seconds, now)
            if not checks["book_fresh"]:
                return fail("stale_order_book")
            spread = book.spread
            checks["spread_ok"] = spread is None or spread <= s.max_spread + 1e-9
            if not checks["spread_ok"]:
                return fail(f"spread_too_wide:{spread:.4f}")

        # --- open order cap -------------------------------------------------
        checks["open_orders_ok"] = open_orders_count < s.max_open_orders
        if not checks["open_orders_ok"]:
            return fail("max_open_orders")

        # --- sizing / exposure ---------------------------------------------
        size = intent.size_usd
        checks["size_positive"] = size > 0
        if size <= 0:
            return fail("non_positive_size")

        strat_cap = s.strategy_max_usd(signal.strategy)
        checks["strategy_size_ok"] = size <= strat_cap + 1e-9
        if not checks["strategy_size_ok"]:
            return fail(f"strategy_size_cap:{size:.2f}>{strat_cap:.2f}")

        # cheap-tail total book cap
        if signal.strategy is StrategyName.cheap_tail:
            tail_book = self.portfolio.strategy_exposure_usd(StrategyName.cheap_tail)
            cap = s.usd(s.max_total_tail_book_pct)
            checks["tail_book_ok"] = tail_book + size <= cap + 1e-9
            if not checks["tail_book_ok"]:
                return fail(f"tail_book_cap:{tail_book + size:.2f}>{cap:.2f}")

        # per-market exposure (use the whole signal notional for multi-leg arb)
        market_notional = signal.suggested_size_usd if signal.legs else size
        mkt_exposure = self.portfolio.market_exposure_usd(intent.market_id)
        mkt_cap = s.usd(s.max_market_exposure_pct)
        room_mkt = mkt_cap - mkt_exposure
        checks["market_exposure_ok"] = room_mkt > 0
        if room_mkt <= 0:
            return fail(f"market_exposure_cap:{mkt_exposure:.2f}>={mkt_cap:.2f}")

        # account exposure
        acct = self.portfolio.total_exposure_usd()
        acct_cap = s.usd(s.max_account_exposure_pct)
        room_acct = acct_cap - acct
        checks["account_exposure_ok"] = room_acct > 0
        if room_acct <= 0:
            return fail(f"account_exposure_cap:{acct:.2f}>={acct_cap:.2f}")

        # daily loss
        dl = self.portfolio.daily_loss_fraction()
        checks["daily_loss_ok"] = dl < s.max_daily_loss_pct
        if not checks["daily_loss_ok"]:
            self.kill.trip(f"daily_loss_limit:{dl:.4f}")
            return fail(f"daily_loss_limit:{dl:.4f}")

        # --- approve, trimming size to the tightest remaining room ---------
        approved = min(size, room_mkt, room_acct)
        if approved < size:
            log.info("trimming %s size %.2f -> %.2f (exposure room)",
                     signal.strategy.value, size, approved)
        if approved <= 0:
            return fail("no_exposure_room")

        self._submissions += 1
        return RiskDecision(allowed=True, reason="ok",
                            approved_size_usd=round(approved, 4), checks=checks)

    # ----------------------------------------------------------------------
    def check_kill_switches(self, latency_ms: float | None = None,
                            position_mismatch: bool = False,
                            wallet_mismatch: bool = False) -> None:
        """Evaluate automatic triggers; trip the kill switch on any anomaly."""
        dl = self.portfolio.daily_loss_fraction()
        if dl >= self.s.max_daily_loss_pct:
            self.kill.trip(f"daily_loss_limit:{dl:.4f}")
        if latency_ms is not None and latency_ms > self.s.max_api_latency_ms:
            self.kill.trip(f"api_latency:{latency_ms:.0f}ms")
        if position_mismatch:
            self.kill.trip("position_mismatch")
        if wallet_mismatch:
            self.kill.trip("wallet_balance_mismatch")
        if self.rejection_rate() > self.s.order_rejection_rate_limit and self._submissions + self._rejections > 20:
            self.kill.trip(f"order_rejection_rate:{self.rejection_rate():.2f}")

    def rejection_rate(self) -> float:
        total = self._submissions + self._rejections
        return self._rejections / total if total else 0.0

    def _record_rejection(self, intent: OrderIntent, reason: str) -> None:
        self._rejections += 1
        log_event(log, logging.DEBUG, f"risk reject: {reason}",
                  stage="risk", reason=reason, token_id=intent.token_id)
        if self.db:
            self.db.save_rejection("risk", reason, {"token_id": intent.token_id})
