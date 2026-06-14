"""SQLite persistence layer.

A thin, dependency-free wrapper around the stdlib ``sqlite3`` module. Stores
every signal, review, order, fill, risk event, and PnL snapshot so the run is
fully auditable after the fact. Schema is created on first connect.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    question TEXT,
    slug TEXT,
    end_timestamp REAL,
    liquidity_usd REAL,
    volume_24h_usd REAL,
    payload TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    market_id TEXT,
    token_id TEXT,
    strategy TEXT,
    side TEXT,
    estimated_fair_price REAL,
    best_bid REAL,
    best_ask REAL,
    edge REAL,
    confidence REAL,
    suggested_size_usd REAL,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS claude_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    market_id TEXT,
    token_id TEXT,
    allow_trade INTEGER,
    risk_level TEXT,
    resolution_ambiguity INTEGER,
    valid_json INTEGER,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    exchange_order_id TEXT,
    ts REAL,
    token_id TEXT,
    side TEXT,
    price REAL,
    size_shares REAL,
    status TEXT,
    strategy TEXT,
    is_paper INTEGER,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    client_order_id TEXT,
    token_id TEXT,
    side TEXT,
    price REAL,
    size_shares REAL,
    fee_usd REAL,
    is_paper INTEGER
);
CREATE TABLE IF NOT EXISTS positions (
    token_id TEXT PRIMARY KEY,
    market_id TEXT,
    strategy TEXT,
    shares REAL,
    avg_price REAL,
    realized_pnl_usd REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    kind TEXT,
    severity TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    realized_pnl_usd REAL,
    unrealized_pnl_usd REAL,
    exposure_usd REAL,
    open_positions INTEGER,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    stage TEXT,
    reason TEXT,
    payload TEXT
);
"""


class Database:
    def __init__(self, path: str = "data/polymanager.db") -> None:
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- low level ----------------------------------------------------------
    def _insert(self, table: str, row: dict[str, Any]) -> None:
        cols = ",".join(row.keys())
        ph = ",".join("?" for _ in row)
        self.conn.execute(
            f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({ph})",
            list(row.values()),
        )
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    # -- typed writers ------------------------------------------------------
    def save_market(self, m: Any) -> None:
        self._insert("markets", {
            "id": m.id, "question": m.question, "slug": m.slug,
            "end_timestamp": m.end_timestamp, "liquidity_usd": m.liquidity_usd,
            "volume_24h_usd": m.volume_24h_usd, "payload": m.model_dump_json(),
            "updated_at": time.time(),
        })

    def save_signal(self, s: Any) -> None:
        self._insert("signals", {
            "ts": time.time(), "market_id": s.market_id, "token_id": s.token_id,
            "strategy": s.strategy.value, "side": s.side.value,
            "estimated_fair_price": s.estimated_fair_price, "best_bid": s.best_bid,
            "best_ask": s.best_ask, "edge": s.edge, "confidence": s.confidence,
            "suggested_size_usd": s.suggested_size_usd, "payload": s.model_dump_json(),
        })

    def save_review(self, market_id: str, token_id: str, r: Any) -> None:
        self._insert("claude_reviews", {
            "ts": time.time(), "market_id": market_id, "token_id": token_id,
            "allow_trade": int(r.allow_trade), "risk_level": r.risk_level.value,
            "resolution_ambiguity": int(r.resolution_ambiguity),
            "valid_json": int(r.valid_json), "payload": r.model_dump_json(),
        })

    def save_order(self, o: Any, is_paper: bool) -> None:
        self._insert("orders", {
            "client_order_id": o.client_order_id, "exchange_order_id": o.exchange_order_id,
            "ts": o.created_at, "token_id": o.token_id, "side": o.side.value,
            "price": o.price, "size_shares": o.size_shares, "status": o.status.value,
            "strategy": o.strategy.value, "is_paper": int(is_paper),
            "payload": o.model_dump_json(),
        })

    def save_fill(self, f: Any) -> None:
        self._insert("fills", {
            "ts": f.timestamp, "client_order_id": f.client_order_id,
            "token_id": f.token_id, "side": f.side.value, "price": f.price,
            "size_shares": f.size_shares, "fee_usd": f.fee_usd, "is_paper": int(f.is_paper),
        })

    def save_position(self, p: Any) -> None:
        self._insert("positions", {
            "token_id": p.token_id, "market_id": p.market_id,
            "strategy": p.strategy.value if p.strategy else None,
            "shares": p.shares, "avg_price": p.avg_price,
            "realized_pnl_usd": p.realized_pnl_usd, "updated_at": time.time(),
        })

    def save_risk_event(self, kind: str, severity: str, detail: dict | str) -> None:
        self._insert("risk_events", {
            "ts": time.time(), "kind": kind, "severity": severity,
            "detail": detail if isinstance(detail, str) else json.dumps(detail),
        })

    def save_rejection(self, stage: str, reason: str, payload: dict | None = None) -> None:
        self._insert("rejections", {
            "ts": time.time(), "stage": stage, "reason": reason,
            "payload": json.dumps(payload or {}),
        })

    def save_pnl_snapshot(self, realized: float, unrealized: float,
                          exposure: float, open_positions: int,
                          extra: dict | None = None) -> None:
        self._insert("pnl_snapshots", {
            "ts": time.time(), "realized_pnl_usd": realized,
            "unrealized_pnl_usd": unrealized, "exposure_usd": exposure,
            "open_positions": open_positions, "payload": json.dumps(extra or {}),
        })

    def close(self) -> None:
        self.conn.close()
