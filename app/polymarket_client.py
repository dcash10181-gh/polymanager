"""Polymarket read-only data gateway.

Read path uses plain ``httpx`` against the public REST endpoints (Gamma for
discovery, CLOB for books/prices, Data API for positions). This keeps the data
layer fully unit-testable with ``respx`` and free of any signing concern.

The authenticated *write* path (order placement/cancellation) lives in
``execution_engine.LiveExecutor`` and uses the official ``py-clob-client`` —
it is never imported here, so read-only mode has zero ability to place orders.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import Settings
from app.logger import get_logger
from app.models import BookLevel, Market, OrderBook, Token

log = get_logger("gateway")


# --------------------------------------------------------------------------
# Parsing helpers — Gamma encodes arrays and numbers as strings.
# --------------------------------------------------------------------------
def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_array(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def parse_market(raw: dict[str, Any]) -> Market:
    """Convert a Gamma market dict into our :class:`Market` model."""
    token_ids = [str(t) for t in _json_array(raw.get("clobTokenIds"))]
    outcomes = [str(o) for o in _json_array(raw.get("outcomes"))]
    prices = [_f(p) for p in _json_array(raw.get("outcomePrices"))]
    tokens: list[Token] = []
    for i, tid in enumerate(token_ids):
        tokens.append(Token(
            token_id=tid,
            outcome=outcomes[i] if i < len(outcomes) else f"outcome_{i}",
            price=prices[i] if i < len(prices) else None,
        ))
    end_date = raw.get("endDate") or raw.get("end_date_iso")
    return Market(
        id=str(raw.get("id") or raw.get("conditionId") or ""),
        condition_id=raw.get("conditionId"),
        question=str(raw.get("question") or raw.get("title") or ""),
        slug=raw.get("slug"),
        description=str(raw.get("description") or ""),
        resolution_source=str(raw.get("resolutionSource") or ""),
        tokens=tokens,
        end_date=end_date,
        end_timestamp=_iso_to_epoch(end_date),
        liquidity_usd=_f(raw.get("liquidityNum", raw.get("liquidity"))),
        volume_24h_usd=_f(raw.get("volume24hr", raw.get("volume24hrClob"))),
        reported_spread=_f(raw.get("spread")) if raw.get("spread") is not None else None,
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        neg_risk=bool(raw.get("negRisk", False)),
        tags=[str(t) for t in _json_array(raw.get("tags"))] if raw.get("tags") else [],
    )


def parse_book(raw: dict[str, Any], token_id: str) -> OrderBook:
    def levels(key: str) -> list[BookLevel]:
        out = []
        for lvl in raw.get(key, []) or []:
            try:
                out.append(BookLevel(price=_f(lvl["price"]), size=_f(lvl["size"])))
            except (KeyError, TypeError):
                continue
        return out

    ts = raw.get("timestamp")
    # CLOB returns ms epoch as a string; fall back to "now" if absent.
    timestamp = _f(ts) / 1000.0 if ts else None
    book = OrderBook(
        token_id=str(raw.get("asset_id") or token_id),
        bids=levels("bids"),
        asks=levels("asks"),
    )
    if timestamp:
        book.timestamp = timestamp
    return book


# --------------------------------------------------------------------------
# Gateway
# --------------------------------------------------------------------------
class PolymarketGateway:
    """Read-only HTTP access to Polymarket public endpoints."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.s = settings
        self.http = client or httpx.Client(
            timeout=httpx.Timeout(10.0),
            headers={"User-Agent": "polymanager/0.1 (+research)"},
        )

    # -- Gamma: discovery ---------------------------------------------------
    def list_markets(self, filters: dict[str, Any] | None = None) -> list[Market]:
        """Fetch active markets from Gamma, paginating up to the configured cap."""
        params: dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": 100,
            "order": "volume24hr",
            "ascending": "false",
        }
        if filters:
            params.update(filters)

        out: list[Market] = []
        offset = 0
        cap = self.s.discovery_max_markets
        while len(out) < cap:
            page = dict(params, offset=offset)
            resp = self.http.get(f"{self.s.gamma_host}/markets", params=page)
            resp.raise_for_status()
            data = resp.json()
            rows = data if isinstance(data, list) else data.get("data", [])
            if not rows:
                break
            out.extend(parse_market(r) for r in rows)
            if len(rows) < params["limit"]:
                break
            offset += params["limit"]
        return out[:cap]

    def get_market(self, market_id: str) -> Market | None:
        resp = self.http.get(f"{self.s.gamma_host}/markets/{market_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            data = data[0] if data else None
        return parse_market(data) if data else None

    # -- CLOB: market data --------------------------------------------------
    def get_orderbook(self, token_id: str) -> OrderBook:
        resp = self.http.get(f"{self.s.polymarket_host}/book",
                             params={"token_id": token_id})
        resp.raise_for_status()
        return parse_book(resp.json(), token_id)

    def get_orderbooks(self, token_ids: list[str]) -> dict[str, OrderBook]:
        """Batch fetch via POST /books; falls back to per-token GET."""
        if not token_ids:
            return {}
        try:
            resp = self.http.post(
                f"{self.s.polymarket_host}/books",
                json={"params": [{"token_id": t} for t in token_ids]},
            )
            resp.raise_for_status()
            books = resp.json()
            result: dict[str, OrderBook] = {}
            for b in books:
                bk = parse_book(b, str(b.get("asset_id", "")))
                result[bk.token_id] = bk
            if result:
                return result
        except (httpx.HTTPError, ValueError):
            pass
        return {t: self.get_orderbook(t) for t in token_ids}

    def get_midpoint(self, token_id: str) -> float | None:
        resp = self.http.get(f"{self.s.polymarket_host}/midpoint",
                             params={"token_id": token_id})
        resp.raise_for_status()
        return _f(resp.json().get("mid")) or None

    def get_spread(self, token_id: str) -> float | None:
        resp = self.http.get(f"{self.s.polymarket_host}/spread",
                             params={"token_id": token_id})
        resp.raise_for_status()
        return _f(resp.json().get("spread"))

    def get_price(self, token_id: str, side: str) -> float | None:
        resp = self.http.get(f"{self.s.polymarket_host}/price",
                             params={"token_id": token_id, "side": side.lower()})
        resp.raise_for_status()
        return _f(resp.json().get("price")) or None

    # -- Data API: account --------------------------------------------------
    def get_positions(self, wallet_address: str | None = None) -> list[dict[str, Any]]:
        wallet = wallet_address or self.s.polymarket_funder_address
        if not wallet:
            return []
        resp = self.http.get(f"{self.s.data_host}/positions",
                             params={"user": wallet})
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])

    def get_recent_trades(self, market_id: str, limit: int = 50) -> list[dict[str, Any]]:
        resp = self.http.get(f"{self.s.data_host}/trades",
                             params={"market": market_id, "limit": limit})
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])

    def close(self) -> None:
        self.http.close()
