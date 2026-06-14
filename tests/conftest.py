"""Shared test fixtures and builders."""

from __future__ import annotations

import time

import pytest

from app.config import Settings
from app.models import BookLevel, Market, OrderBook, Token


@pytest.fixture
def settings() -> Settings:
    # Construct directly (ignore any ambient .env) for deterministic tests.
    return Settings(
        _env_file=None,
        bankroll_usd=1000.0,
        reasoner_disabled=True,
        min_liquidity_usd=1000.0,
        min_24h_volume_usd=100.0,
    )


def make_book(token_id: str, bid: float, ask: float,
              bid_size: float = 1000.0, ask_size: float = 1000.0,
              ts: float | None = None) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=[BookLevel(price=bid, size=bid_size)],
        asks=[BookLevel(price=ask, size=ask_size)],
        timestamp=ts if ts is not None else time.time(),
    )


def make_binary_market(mid: str = "m1", yes: str = "YES", no: str = "NO") -> Market:
    return Market(
        id=mid,
        question="Will it happen?",
        description="Resolves YES if the official source reports it.",
        resolution_source="official",
        tokens=[Token(token_id=yes, outcome="Yes"), Token(token_id=no, outcome="No")],
        liquidity_usd=50_000.0,
        volume_24h_usd=10_000.0,
        end_timestamp=time.time() + 86_400,
    )


@pytest.fixture
def binary_market() -> Market:
    return make_binary_market()
