"""Market discovery and watchlist construction.

Scans Gamma for active markets and keeps only those that clear minimum
liquidity / volume / spread / time-to-resolution standards. Markets the bot
cannot trade safely (illiquid, wide-spread, about-to-resolve, or with obviously
ambiguous wording) are filtered out before they ever reach the signal engine.
"""

from __future__ import annotations

import time

from app.config import Settings
from app.logger import get_logger, log_event
import logging
from app.models import Market
from app.polymarket_client import PolymarketGateway

log = get_logger("discovery")

# Phrases that hint at subjective/ambiguous resolution — demote early in dev.
_AMBIGUITY_HINTS = (
    "subjective", "at the discretion", "to be determined", "tbd",
    "credible reports", "widely reported", "generally considered",
)


class MarketDiscovery:
    def __init__(self, gateway: PolymarketGateway, settings: Settings) -> None:
        self.gateway = gateway
        self.s = settings
        self._watchlist: list[Market] = []
        self._last_refresh: float = 0.0

    def passes_filters(self, m: Market, now: float | None = None) -> tuple[bool, str]:
        now = now if now is not None else time.time()
        if not m.active or m.closed:
            return False, "inactive_or_closed"
        if not m.tokens:
            return False, "no_tokens"
        if m.liquidity_usd < self.s.min_liquidity_usd:
            return False, f"liquidity {m.liquidity_usd:.0f} < {self.s.min_liquidity_usd:.0f}"
        if m.volume_24h_usd < self.s.min_24h_volume_usd:
            return False, f"volume {m.volume_24h_usd:.0f} < {self.s.min_24h_volume_usd:.0f}"
        if m.reported_spread is not None and m.reported_spread > self.s.max_spread:
            return False, f"spread {m.reported_spread:.3f} > {self.s.max_spread:.3f}"
        secs = m.seconds_to_resolution(now)
        if secs is not None and secs <= 0:
            return False, "already_resolved"
        # Very near-term resolution (<60s) is risky for non-arb strategies.
        if secs is not None and secs < 60:
            return False, "resolving_imminently"
        desc = (m.description or "").lower()
        if any(h in desc for h in _AMBIGUITY_HINTS):
            return False, "ambiguous_wording_hint"
        return True, "ok"

    def refresh(self, extra_filters: dict | None = None) -> list[Market]:
        markets = self.gateway.list_markets(extra_filters)
        kept: list[Market] = []
        rejected = 0
        for m in markets:
            ok, _reason = self.passes_filters(m)
            if ok:
                kept.append(m)
            else:
                rejected += 1
        # Rank by a simple tradeability proxy: liquidity * volume.
        kept.sort(key=lambda m: m.liquidity_usd * (m.volume_24h_usd + 1), reverse=True)
        self._watchlist = kept
        self._last_refresh = time.time()
        log_event(log, logging.INFO,
                  f"watchlist refreshed: {len(kept)} kept / {rejected} rejected "
                  f"of {len(markets)}",
                  kept=len(kept), rejected=rejected, scanned=len(markets))
        return kept

    def get_watchlist(self, max_age_seconds: float = 60.0) -> list[Market]:
        if not self._watchlist or (time.time() - self._last_refresh) > max_age_seconds:
            self.refresh()
        return self._watchlist
