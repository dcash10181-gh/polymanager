"""Self-contained PAPER-MODE demo — no funds, no keys, no network.

Drives the full live pipeline (discovery -> signals -> Claude gate -> risk
manager -> paper broker -> exit manager) against a synthetic market whose price
drifts upward, then prints the performance report. Proves you can exercise the
whole bot end-to-end without risking a cent.

Run:  python -m scripts.demo_paper
"""

from __future__ import annotations

import os
import tempfile
import time

from app.config import BotMode, Settings
from app.db import Database
from app.main import TradingApp
from app.models import BookLevel, Market, OrderBook, Token
from app.report import compute_metrics, render_text


class DemoGateway:
    """Fake read-only gateway: one crypto market with an upward-drifting book."""

    def __init__(self) -> None:
        self.market = Market(
            id="btc-100k",
            question="Will Bitcoin be above $100,000 in 3 days?",
            description="Resolves YES per the official close. Clear, machine-readable.",
            resolution_source="official",
            tokens=[Token(token_id="YES", outcome="Yes"),
                    Token(token_id="NO", outcome="No")],
            liquidity_usd=200_000.0, volume_24h_usd=50_000.0,
            end_timestamp=time.time() + 3 * 86_400,
        )
        # YES mid price path over successive polls — the "market catching up".
        self.path = [0.55, 0.57, 0.60, 0.63, 0.66, 0.66]
        self.step = 0

    def list_markets(self, filters=None):
        return [self.market]

    def get_orderbooks(self, token_ids):
        mid = self.path[min(self.step, len(self.path) - 1)]
        self.step += 1
        half = 0.005
        now = time.time()

        def book(tid, m):
            return OrderBook(
                token_id=tid,
                bids=[BookLevel(price=round(m - half, 3), size=5000)],
                asks=[BookLevel(price=round(m + half, 3), size=5000)],
                timestamp=now)

        all_books = {"YES": book("YES", mid), "NO": book("NO", 1 - mid)}
        return {t: b for t, b in all_books.items() if t in token_ids}

    def get_positions(self, wallet=None):
        return []

    def close(self):
        pass


def main() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "demo.db")
    settings = Settings(
        _env_file=None,
        bot_mode=BotMode.paper,        # <-- paper: simulated fills, no funds
        reasoner_disabled=True,        # <-- offline: no Anthropic key needed
        db_path=db_path,
        bankroll_usd=1000.0,
        loop_interval_seconds=0.0,     # no real-time waiting in the demo
        min_liquidity_usd=1000.0,
        min_24h_volume_usd=100.0,
    )

    app = TradingApp(settings, gateway=DemoGateway())
    # Operator alpha: a fair value ahead of the lagging book (board-behind-the-move).
    app.external_provider = lambda m: {"fair_price:YES": 0.70}

    print(">>> running 6 paper-mode passes against synthetic data (no funds)...\n")
    app.run(iterations=6)

    snap = app.portfolio.snapshot()
    print(f"\n>>> paper portfolio: {snap}\n")

    db = Database(db_path)
    try:
        print(render_text(compute_metrics(db)))
    finally:
        db.close()
    print(f"\n(audit log written to {db_path})")


if __name__ == "__main__":
    main()
