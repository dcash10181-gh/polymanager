import time

from app.config import StrategyName
from app.db import Database
from app.models import Fill, Order, OrderStatus, Side, Signal
from app.report import compute_metrics, render_text


def _order(cid, token, side, price, strategy):
    return Order(client_order_id=cid, token_id=token, side=side, price=price,
                 size_shares=100.0, status=OrderStatus.filled, strategy=strategy)


def _fill(cid, token, side, price, ts):
    return Fill(client_order_id=cid, token_id=token, side=side, price=price,
                size_shares=100.0, fee_usd=0.0, is_paper=True, timestamp=ts)


def _seed_db() -> Database:
    db = Database(":memory:")
    t = time.time()
    # momentum: buy A @0.50, sell A @0.60 -> +10 (win)
    db.save_order(_order("o1", "A", Side.BUY, 0.50, StrategyName.momentum_lag), True)
    db.save_order(_order("o2", "A", Side.SELL, 0.60, StrategyName.momentum_lag), True)
    db.save_fill(_fill("o1", "A", Side.BUY, 0.50, t))
    db.save_fill(_fill("o2", "A", Side.SELL, 0.60, t + 1))
    # near-certainty: buy B @0.90, sell B @0.85 -> -5 (loss)
    db.save_order(_order("o3", "B", Side.BUY, 0.90, StrategyName.near_certainty), True)
    db.save_order(_order("o4", "B", Side.SELL, 0.85, StrategyName.near_certainty), True)
    db.save_fill(_fill("o3", "B", Side.BUY, 0.90, t + 2))
    db.save_fill(_fill("o4", "B", Side.SELL, 0.85, t + 3))
    # a couple signals + a rejection + a review
    db.save_signal(Signal(market_id="m", token_id="A", strategy=StrategyName.momentum_lag,
                          side=Side.BUY, estimated_fair_price=0.6, edge=0.05,
                          max_price=0.55, suggested_size_usd=10.0))
    db.save_rejection("risk", "spread_too_wide", {"token_id": "A"})
    return db


def test_realized_pnl_by_strategy():
    db = _seed_db()
    m = compute_metrics(db)
    mom = m["by_strategy"]["momentum_lag"]
    nc = m["by_strategy"]["near_certainty"]
    assert abs(mom["realized_pnl_usd"] - 10.0) < 1e-6
    assert abs(nc["realized_pnl_usd"] - (-5.0)) < 1e-6
    assert abs(m["overall"]["realized_pnl_usd"] - 5.0) < 1e-6


def test_win_loss_and_fill_rate():
    db = _seed_db()
    m = compute_metrics(db)
    mom = m["by_strategy"]["momentum_lag"]
    nc = m["by_strategy"]["near_certainty"]
    assert mom["closes"] == 1 and mom["wins"] == 1 and mom["win_rate"] == 1.0
    assert nc["closes"] == 1 and nc["losses"] == 1 and nc["win_rate"] == 0.0
    # 2 orders each, both filled
    assert mom["orders"] == 2 and mom["filled_orders"] == 2 and mom["fill_rate"] == 1.0


def test_volume_and_fees():
    db = _seed_db()
    m = compute_metrics(db)
    # momentum volume = 100*0.50 + 100*0.60 = 110
    assert abs(m["by_strategy"]["momentum_lag"]["volume_usd"] - 110.0) < 1e-6
    assert m["overall"]["fees_usd"] == 0.0


def test_signals_and_rejections_counted():
    db = _seed_db()
    m = compute_metrics(db)
    assert m["by_strategy"]["momentum_lag"]["signals"] == 1
    assert m["rejections"].get("spread_too_wide") == 1


def test_render_text_runs():
    db = _seed_db()
    out = render_text(compute_metrics(db))
    assert "OVERALL" in out
    assert "momentum_lag" in out


def test_empty_db_is_safe():
    db = Database(":memory:")
    m = compute_metrics(db)
    assert m["overall"]["realized_pnl_usd"] == 0.0
    assert render_text(m)  # does not crash
