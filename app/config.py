"""Configuration loading and validation.

All tunable parameters live here, loaded from environment / ``.env`` via
pydantic-settings. The single source of truth for risk limits, strategy
thresholds, and operating mode.

Safety invariants enforced here:
  * ``BOT_MODE`` defaults to ``paper`` and can never *default* to live.
  * Live/shadow modes require credentials to be present (validated lazily by
    the gateway, not here, so paper mode works with zero secrets).
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotMode(str, Enum):
    """Operating modes, from safest to most permissive."""

    research = "research"      # fetch + score only, never any order
    paper = "paper"            # simulate fills against the paper broker
    shadow = "shadow"          # real data + real account, emit intents, no orders
    tiny_live = "tiny_live"    # real orders, tiny size, one strategy
    live = "live"              # real orders, scaled
    paused = "paused"          # kill-switch state, no new entries

    @property
    def places_real_orders(self) -> bool:
        return self in (BotMode.tiny_live, BotMode.live)

    @property
    def is_paper(self) -> bool:
        return self is BotMode.paper

    @property
    def needs_credentials(self) -> bool:
        """Modes that talk to the authenticated CLOB endpoints."""
        return self in (BotMode.shadow, BotMode.tiny_live, BotMode.live)


class StrategyName(str, Enum):
    near_certainty = "near_certainty"
    momentum_lag = "momentum_lag"
    cheap_tail = "cheap_tail"
    arbitrage = "arbitrage"


class Settings(BaseSettings):
    """Typed application settings. Field names map to UPPER_SNAKE env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Credentials & hosts ------------------------------------------------
    polymarket_private_key: str | None = None
    polymarket_funder_address: str | None = None
    polymarket_signature_type: int = 1
    polymarket_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_host: str = "https://data-api.polymarket.com"
    chain_id: int = 137

    anthropic_api_key: str | None = None
    reasoner_model: str = "claude-sonnet-4-6"
    reasoner_disabled: bool = False

    # --- Mode & bankroll ----------------------------------------------------
    bot_mode: BotMode = BotMode.paper
    bankroll_usd: float = 1000.0

    # --- Account risk limits (fractions of bankroll) ------------------------
    max_account_exposure_pct: float = 0.25
    max_market_exposure_pct: float = 0.02
    max_daily_loss_pct: float = 0.04
    max_open_orders: int = 20
    min_edge_bps: int = 35

    # --- Per-strategy position caps (fractions of bankroll) -----------------
    max_momentum_position_pct: float = 0.005
    max_near_certainty_position_pct: float = 0.01
    max_tail_position_pct: float = 0.003
    max_total_tail_book_pct: float = 0.05
    max_arbitrage_position_pct: float = 0.02

    # --- Discovery filters --------------------------------------------------
    min_liquidity_usd: float = 5000.0
    min_24h_volume_usd: float = 1000.0
    max_spread: float = 0.03
    max_order_book_age_seconds: float = 2.0
    stale_order_cancel_seconds: float = 10.0
    discovery_max_markets: int = 300

    # --- Strategy thresholds ------------------------------------------------
    # Near-certainty
    nc_enter_price_min: float = 0.980
    nc_enter_price_max: float = 0.995
    nc_risk_buffer: float = 0.005          # required_q >= price + buffer
    nc_take_profit: float = 0.998
    nc_stop_loss: float = 0.965
    nc_min_depth_usd: float = 100.0
    # Momentum lag
    mom_min_edge: float = 0.01
    mom_max_spread: float = 0.02
    mom_take_profit: float = 0.02
    mom_stop_loss: float = 0.02
    mom_time_stop_seconds: int = 300
    mom_min_external_move_score: float = 0.5
    mom_min_depth_usd: float = 50.0
    # Cheap tail
    tail_enter_price_min: float = 0.010
    tail_enter_price_max: float = 0.050
    tail_prob_multiple: float = 1.75       # est_prob >= ask * multiple
    tail_take_profit_multiple: float = 2.0
    tail_stop_loss_fraction: float = 0.5   # exit at 50% of entry
    tail_min_depth_usd: float = 25.0
    # Arbitrage
    arb_min_edge: float = 0.005            # required net edge after costs
    arb_min_depth_usd: float = 50.0

    # --- Execution / loop ---------------------------------------------------
    loop_interval_seconds: float = 2.0
    max_api_latency_ms: float = 1500.0
    order_rejection_rate_limit: float = 0.25
    fee_bps: float = 0.0                   # Polymarket maker/taker fee (bps)
    order_slice_usd: float = 50.0          # split larger orders into slices
    min_order_size_usd: float = 1.0        # skip live orders below this notional

    # --- Alpha / external signal providers ----------------------------------
    enable_crypto_alpha: bool = False      # wire the crypto-threshold provider
    alpha_price_host: str = "https://api.coinbase.com"
    alpha_catalyst_horizon_days: float = 7.0

    # --- Storage / logging --------------------------------------------------
    db_path: str = "data/polymanager.db"
    log_dir: str = "logs"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _sanity(self) -> "Settings":
        if not (0 < self.max_account_exposure_pct <= 1):
            raise ValueError("max_account_exposure_pct must be in (0, 1]")
        if not (0 < self.max_daily_loss_pct <= 1):
            raise ValueError("max_daily_loss_pct must be in (0, 1]")
        if self.bankroll_usd <= 0:
            raise ValueError("bankroll_usd must be positive")
        if self.nc_enter_price_min >= self.nc_enter_price_max:
            raise ValueError("nc_enter_price_min must be < nc_enter_price_max")
        return self

    # --- Convenience --------------------------------------------------------
    def usd(self, pct: float) -> float:
        """Convert a bankroll fraction into a USD amount."""
        return round(self.bankroll_usd * pct, 4)

    def strategy_max_usd(self, strategy: StrategyName) -> float:
        mapping = {
            StrategyName.near_certainty: self.max_near_certainty_position_pct,
            StrategyName.momentum_lag: self.max_momentum_position_pct,
            StrategyName.cheap_tail: self.max_tail_position_pct,
            StrategyName.arbitrage: self.max_arbitrage_position_pct,
        }
        return self.usd(mapping[strategy])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton settings instance."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the cache and reload (used by tests and config hot-reload)."""
    get_settings.cache_clear()
    return get_settings()
