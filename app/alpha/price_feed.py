"""Live crypto spot-price feed.

Pulls USD spot from a public, no-auth endpoint (Coinbase by default) over
``httpx`` with a short TTL cache. The HTTP client is injectable so tests run
fully offline. On any error it serves the last cached value (or None).
"""

from __future__ import annotations

import time

import httpx

from app.logger import get_logger

log = get_logger("alpha.feed")


class PriceFeed:
    def __init__(self, base_url: str = "https://api.coinbase.com",
                 client: httpx.Client | None = None, ttl_seconds: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.Client(
            timeout=httpx.Timeout(5.0),
            headers={"User-Agent": "polymanager/0.1"},
        )
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, ts)

    def spot(self, symbol: str) -> float | None:
        """USD spot price for a base symbol like ``BTC`` (cached up to ttl)."""
        symbol = symbol.upper()
        now = time.time()
        cached = self._cache.get(symbol)
        if cached and now - cached[1] < self.ttl:
            return cached[0]
        try:
            resp = self.http.get(f"{self.base_url}/v2/prices/{symbol}-USD/spot")
            resp.raise_for_status()
            price = float(resp.json()["data"]["amount"])
        except Exception as e:
            log.warning("price fetch failed for %s: %s", symbol, e)
            return cached[0] if cached else None
        self._cache[symbol] = (price, now)
        return price

    def close(self) -> None:
        self.http.close()
