"""Performance / PnL report over the SQLite audit log.

Reconstructs realized PnL per strategy by replaying recorded fills through the
same position accounting used live, and rolls up the Section 22 metrics:
signals, attempted orders, fill rate, win rate, average win/loss, realized PnL,
fees, volume, and EV per dollar. Also summarizes rejection reasons and the
Claude review allow-rate.

Usage::

    python -m app.report                 # uses DB_PATH from .env
    python -m app.report --db data/x.db
    python -m app.report --json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from app.config import get_settings
from app.db import Database
from app.models import Fill, Position, Side


@dataclass
class StrategyStats:
    signals: int = 0
    orders: int = 0
    filled_orders: int = 0
    fills: int = 0
    realized_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    volume_usd: float = 0.0
    closes: int = 0
    wins: int = 0
    losses: int = 0
    sum_win_usd: float = 0.0
    sum_loss_usd: float = 0.0

    @property
    def fill_rate(self) -> float:
        return self.filled_orders / self.orders if self.orders else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.closes if self.closes else 0.0

    @property
    def avg_win_usd(self) -> float:
        return self.sum_win_usd / self.wins if self.wins else 0.0

    @property
    def avg_loss_usd(self) -> float:
        return self.sum_loss_usd / self.losses if self.losses else 0.0

    @property
    def ev_per_dollar(self) -> float:
        return self.realized_pnl_usd / self.volume_usd if self.volume_usd else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(fill_rate=round(self.fill_rate, 4), win_rate=round(self.win_rate, 4),
                 avg_win_usd=round(self.avg_win_usd, 4),
                 avg_loss_usd=round(self.avg_loss_usd, 4),
                 ev_per_dollar=round(self.ev_per_dollar, 6))
        for k in ("realized_pnl_usd", "fees_usd", "volume_usd",
                  "sum_win_usd", "sum_loss_usd"):
            d[k] = round(d[k], 4)
        return d


def compute_metrics(db: Database) -> dict:
    stats: dict[str, StrategyStats] = defaultdict(StrategyStats)

    # strategy per order, and per-strategy order counts
    strat_by_order: dict[str, str] = {}
    for r in db.query("SELECT client_order_id, strategy FROM orders"):
        strat_by_order[r["client_order_id"]] = r["strategy"] or "unknown"
        stats[r["strategy"] or "unknown"].orders += 1

    for r in db.query("SELECT strategy, COUNT(*) c FROM signals GROUP BY strategy"):
        stats[r["strategy"] or "unknown"].signals += r["c"]

    # Replay fills in time order, attributing realized PnL to each fill's strategy.
    positions: dict[str, Position] = {}
    filled_orders: dict[str, set] = defaultdict(set)
    for f in db.query("SELECT * FROM fills ORDER BY ts ASC, id ASC"):
        strat = strat_by_order.get(f["client_order_id"], "unknown")
        st = stats[strat]
        side = Side(f["side"])
        pos = positions.setdefault(f["token_id"], Position(token_id=f["token_id"]))
        reducing = (pos.shares > 0 and side is Side.SELL) or \
                   (pos.shares < 0 and side is Side.BUY)
        before = pos.realized_pnl_usd
        fill = Fill(client_order_id=f["client_order_id"], token_id=f["token_id"],
                    side=side, price=f["price"], size_shares=f["size_shares"],
                    fee_usd=f["fee_usd"], is_paper=bool(f["is_paper"]))
        pos.apply_fill(fill)
        delta = pos.realized_pnl_usd - before

        st.fills += 1
        st.fees_usd += fill.fee_usd
        st.volume_usd += fill.notional_usd
        st.realized_pnl_usd += delta
        filled_orders[strat].add(f["client_order_id"])
        if reducing:
            st.closes += 1
            if delta >= 0:
                st.wins += 1
                st.sum_win_usd += delta
            else:
                st.losses += 1
                st.sum_loss_usd += delta

    for strat, ids in filled_orders.items():
        stats[strat].filled_orders = len(ids)

    # rejections + claude review allow-rate
    rejections = {r["reason"]: r["c"] for r in db.query(
        "SELECT reason, COUNT(*) c FROM rejections GROUP BY reason ORDER BY c DESC LIMIT 15")}
    review_rows = db.query(
        "SELECT COUNT(*) n, SUM(allow_trade) a, SUM(resolution_ambiguity) amb, "
        "SUM(valid_json) vj FROM claude_reviews")
    rv = review_rows[0] if review_rows else None
    reviews = {
        "total": (rv["n"] or 0) if rv else 0,
        "allowed": (rv["a"] or 0) if rv else 0,
        "ambiguous": (rv["amb"] or 0) if rv else 0,
        "invalid_json": ((rv["n"] or 0) - (rv["vj"] or 0)) if rv else 0,
    }
    if reviews["total"]:
        reviews["allow_rate"] = round(reviews["allowed"] / reviews["total"], 4)

    overall = StrategyStats()
    for st in stats.values():
        overall.signals += st.signals
        overall.orders += st.orders
        overall.filled_orders += st.filled_orders
        overall.fills += st.fills
        overall.realized_pnl_usd += st.realized_pnl_usd
        overall.fees_usd += st.fees_usd
        overall.volume_usd += st.volume_usd
        overall.closes += st.closes
        overall.wins += st.wins
        overall.losses += st.losses
        overall.sum_win_usd += st.sum_win_usd
        overall.sum_loss_usd += st.sum_loss_usd

    return {
        "by_strategy": {k: v.to_dict() for k, v in sorted(stats.items())},
        "overall": overall.to_dict(),
        "rejections": rejections,
        "reviews": reviews,
    }


def render_text(metrics: dict) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("POLYMANAGER PERFORMANCE REPORT")
    lines.append("=" * 78)
    hdr = (f"{'strategy':<16}{'sig':>5}{'ord':>5}{'fill%':>7}{'closes':>8}"
           f"{'win%':>7}{'realized$':>12}{'EV/$':>9}")
    lines.append(hdr)
    lines.append("-" * 78)
    for name, s in metrics["by_strategy"].items():
        lines.append(
            f"{name:<16}{s['signals']:>5}{s['orders']:>5}"
            f"{s['fill_rate']*100:>6.0f}%{s['closes']:>8}"
            f"{s['win_rate']*100:>6.0f}%{s['realized_pnl_usd']:>12.2f}"
            f"{s['ev_per_dollar']:>9.4f}")
    o = metrics["overall"]
    lines.append("-" * 78)
    lines.append(
        f"{'OVERALL':<16}{o['signals']:>5}{o['orders']:>5}"
        f"{o['fill_rate']*100:>6.0f}%{o['closes']:>8}"
        f"{o['win_rate']*100:>6.0f}%{o['realized_pnl_usd']:>12.2f}"
        f"{o['ev_per_dollar']:>9.4f}")
    lines.append("")
    lines.append(f"fees paid: ${o['fees_usd']:.2f}   volume: ${o['volume_usd']:.2f}   "
                 f"avg win: ${o['avg_win_usd']:.2f}   avg loss: ${o['avg_loss_usd']:.2f}")
    rv = metrics["reviews"]
    if rv["total"]:
        lines.append(f"claude reviews: {rv['total']} total, "
                     f"{rv.get('allow_rate', 0)*100:.0f}% allowed, "
                     f"{rv['ambiguous']} ambiguous, {rv['invalid_json']} invalid")
    if metrics["rejections"]:
        lines.append("")
        lines.append("top rejection reasons:")
        for reason, c in list(metrics["rejections"].items())[:10]:
            lines.append(f"  {c:>5}  {reason}")
    lines.append("=" * 78)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymanager performance report")
    parser.add_argument("--db", default=None, help="path to the SQLite DB")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    db_path = args.db or get_settings().db_path
    db = Database(db_path)
    try:
        metrics = compute_metrics(db)
    finally:
        db.close()

    if args.json:
        print(json.dumps(metrics, indent=2))
    else:
        print(render_text(metrics))


if __name__ == "__main__":
    main()
