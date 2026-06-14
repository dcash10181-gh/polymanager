import time

from app.models import BookLevel, Fill, OrderBook, OrderIntent, Position, Side
from app.config import StrategyName


def test_orderbook_derived_quantities():
    ob = OrderBook(
        token_id="t",
        bids=[BookLevel(price=0.67, size=200), BookLevel(price=0.68, size=100)],
        asks=[BookLevel(price=0.70, size=50), BookLevel(price=0.69, size=150)],
    )
    # best computed defensively regardless of wire order
    assert ob.best_bid == 0.68
    assert ob.best_ask == 0.69
    assert ob.midpoint == 0.685
    assert abs(ob.spread - 0.01) < 1e-9
    assert ob.has_two_sides()


def test_orderbook_depth_within():
    ob = OrderBook(
        token_id="t",
        bids=[BookLevel(price=0.68, size=100), BookLevel(price=0.60, size=1000)],
        asks=[BookLevel(price=0.69, size=100), BookLevel(price=0.72, size=1000)],
    )
    # only the top level is within 0.005 of best
    assert abs(ob.ask_depth_usd(within=0.005) - 0.69 * 100) < 1e-6
    assert abs(ob.bid_depth_usd(within=0.005) - 0.68 * 100) < 1e-6


def test_orderbook_staleness():
    ob = OrderBook(token_id="t", timestamp=time.time() - 10)
    assert ob.is_stale(max_age=2)
    fresh = OrderBook(token_id="t", timestamp=time.time())
    assert not fresh.is_stale(max_age=2)


def test_position_weighted_average_and_realized_pnl():
    p = Position(token_id="t")
    p.apply_fill(Fill(client_order_id="a", token_id="t", side=Side.BUY, price=0.50, size_shares=100))
    p.apply_fill(Fill(client_order_id="b", token_id="t", side=Side.BUY, price=0.60, size_shares=100))
    assert abs(p.avg_price - 0.55) < 1e-9
    assert p.shares == 200
    # sell 100 @ 0.70 -> realize (0.70-0.55)*100 = 15
    p.apply_fill(Fill(client_order_id="c", token_id="t", side=Side.SELL, price=0.70, size_shares=100))
    assert abs(p.realized_pnl_usd - 15.0) < 1e-9
    assert p.shares == 100
    assert abs(p.avg_price - 0.55) < 1e-9


def test_position_close_to_zero():
    p = Position(token_id="t")
    p.apply_fill(Fill(client_order_id="a", token_id="t", side=Side.BUY, price=0.40, size_shares=100))
    p.apply_fill(Fill(client_order_id="b", token_id="t", side=Side.SELL, price=0.45, size_shares=100))
    assert abs(p.shares) < 1e-9
    assert p.avg_price == 0.0
    assert abs(p.realized_pnl_usd - 5.0) < 1e-9


def test_position_fee_reduces_pnl():
    p = Position(token_id="t")
    p.apply_fill(Fill(client_order_id="a", token_id="t", side=Side.BUY, price=0.50,
                      size_shares=100, fee_usd=1.0))
    assert abs(p.realized_pnl_usd - (-1.0)) < 1e-9


def test_order_intent_size_shares():
    oi = OrderIntent(market_id="m", token_id="t", side=Side.BUY, price=0.50,
                     size_usd=10.0, strategy=StrategyName.near_certainty)
    assert oi.size_shares == 20.0
