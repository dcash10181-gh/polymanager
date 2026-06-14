"""Crypto threshold-market alpha provider.

For markets like "Will Bitcoin be above $100,000 on Dec 31?" it parses the
asset, direction, and strike, fetches live spot, and prices the YES/NO outcomes
with the lognormal model in :mod:`app.alpha.pricing`. As spot moves, the fair
value updates — exactly the "board behind the move" edge the momentum and
near-certainty strategies are built for.

Limitations (documented, intentionally conservative):
  * "reach"/"hit" markets are touch options; we price them as terminal
    above/below, which *under*-states their probability — so we trade them less,
    not more. Tune later with a barrier model.
  * Per-asset annualized volatility is configured, not estimated. Override via
    ``vols`` when you have a better estimate.
"""

from __future__ import annotations

import re

from app.alpha.base import ExternalSignalProvider
from app.alpha.price_feed import PriceFeed
from app.alpha.pricing import prob_above
from app.config import Settings
from app.logger import get_logger
from app.models import Market

log = get_logger("alpha.crypto")

_SECONDS_PER_YEAR = 365.25 * 86_400

# question token -> canonical symbol
_ASSETS = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "ether": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
    "dogecoin": "DOGE", "doge": "DOGE",
    "ripple": "XRP", "xrp": "XRP",
    "bnb": "BNB",
    "cardano": "ADA", "ada": "ADA",
    "litecoin": "LTC", "ltc": "LTC",
    "avalanche": "AVAX", "avax": "AVAX",
}

_DEFAULT_VOLS = {
    "BTC": 0.60, "ETH": 0.75, "SOL": 1.00, "DOGE": 1.20, "XRP": 1.00,
    "BNB": 0.80, "ADA": 1.00, "LTC": 0.90, "AVAX": 1.10,
}

_ABOVE_WORDS = ("above", "over", "exceed", "greater", "reach", "hit",
                "more than", "at least", "≥", ">=", ">")
_BELOW_WORDS = ("below", "under", "less than", "dip", "drop below",
                "fall below", "≤", "<=", "<")

_AMOUNT_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM]?)")


def _parse_amount(token: str, suffix: str) -> float:
    val = float(token.replace(",", ""))
    if suffix.lower() == "k":
        val *= 1_000
    elif suffix.lower() == "m":
        val *= 1_000_000
    return val


def parse_question(question: str) -> tuple[str, str, float] | None:
    """Return (symbol, direction, strike) or None if not a crypto threshold."""
    q = question.lower()
    symbol = None
    for word, sym in _ASSETS.items():
        if re.search(rf"\b{re.escape(word)}\b", q):
            symbol = sym
            break
    if symbol is None:
        return None

    m = _AMOUNT_RE.search(question)
    if not m:
        return None
    strike = _parse_amount(m.group(1), m.group(2))
    if strike <= 0:
        return None

    # Direction: prefer the keyword closest before the dollar amount.
    direction = "above"
    if any(w in q for w in _BELOW_WORDS) and not any(w in q for w in ("above", "over", "exceed")):
        direction = "below"
    return symbol, direction, strike


class CryptoThresholdProvider(ExternalSignalProvider):
    def __init__(self, settings: Settings, price_feed: PriceFeed,
                 vols: dict[str, float] | None = None,
                 catalyst_horizon_days: float = 7.0) -> None:
        self.s = settings
        self.feed = price_feed
        self.vols = {**_DEFAULT_VOLS, **(vols or {})}
        self.catalyst_horizon_days = catalyst_horizon_days

    def signals(self, market: Market) -> dict:
        parsed = parse_question(market.question or "")
        if parsed is None:
            return {}
        symbol, direction, strike = parsed

        spot = self.feed.spot(symbol)
        if spot is None:
            return {}

        tau = market.seconds_to_resolution()
        if tau is None or tau <= 0:
            return {}
        tau_years = tau / _SECONDS_PER_YEAR
        sigma = self.vols.get(symbol, 0.80)

        p_above = prob_above(spot, strike, sigma, tau_years)
        if p_above is None:
            return {}
        yes_prob = p_above if direction == "above" else (1.0 - p_above)
        yes_prob = max(0.0, min(1.0, yes_prob))

        yes = market.token_for_outcome("Yes") or (market.tokens[0] if market.tokens else None)
        no = market.token_for_outcome("No")
        catalyst = tau <= self.catalyst_horizon_days * 86_400

        out: dict = {}
        if yes is not None:
            out[f"fair_price:{yes.token_id}"] = yes_prob
            out[f"true_prob:{yes.token_id}"] = yes_prob
            out[f"catalyst:{yes.token_id}"] = catalyst
        if no is not None:
            out[f"fair_price:{no.token_id}"] = 1.0 - yes_prob
            out[f"true_prob:{no.token_id}"] = 1.0 - yes_prob
            out[f"catalyst:{no.token_id}"] = catalyst
        log.debug("crypto alpha %s spot=%.2f strike=%.2f %s -> yes_prob=%.4f",
                  symbol, spot, strike, direction, yes_prob)
        return out
