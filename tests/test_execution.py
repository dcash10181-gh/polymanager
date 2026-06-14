from app.config import Settings, StrategyName
from app.execution_engine import ExecutionEngine, TradeIntentBuilder, snap_price
from app.models import Side, Signal, SignalLeg
from app.portfolio import Portfolio


def test_snap_price_buy_floors_sell_ceils():
    # 0.5749 on a 0.01 grid -> buy floors to 0.57, sell ceils to 0.58
    assert snap_price(0.5749, 0.01, Side.BUY) == 0.57
    assert snap_price(0.5749, 0.01, Side.SELL) == 0.58
    # finer grid
    assert snap_price(0.5744, 0.001, Side.BUY) == 0.574
    assert snap_price(0.5744, 0.001, Side.SELL) == 0.575


def test_snap_price_clamps_to_grid_edges():
    assert snap_price(0.0001, 0.01, Side.BUY) == 0.01
    assert snap_price(0.9999, 0.01, Side.SELL) == 0.99


def test_intent_builder_single():
    sig = Signal(market_id="m", token_id="t", strategy=StrategyName.momentum_lag,
                 side=Side.BUY, estimated_fair_price=0.6, edge=0.05, max_price=0.59,
                 suggested_size_usd=10.0)
    intents = TradeIntentBuilder.build(sig, approved_size_usd=4.0)
    assert len(intents) == 1
    assert intents[0].size_usd == 4.0
    assert intents[0].price == 0.59  # limits at the worst acceptable price


def test_intent_builder_arbitrage_legs_scaled():
    # suggested_size_usd equals the sum of leg notionals (as the engine builds it)
    sig = Signal(market_id="m", token_id="YES", strategy=StrategyName.arbitrage,
                 side=Side.BUY, estimated_fair_price=1.0, edge=0.03, max_price=0.57,
                 suggested_size_usd=97.0,
                 legs=[SignalLeg(token_id="YES", side=Side.BUY, price=0.57, size_usd=57.0),
                       SignalLeg(token_id="NO", side=Side.BUY, price=0.40, size_usd=40.0)])
    # approve half the basket -> legs scale proportionally
    intents = TradeIntentBuilder.build(sig, approved_size_usd=48.5)
    assert len(intents) == 2
    assert abs(sum(i.size_usd for i in intents) - 48.5) < 0.5


def test_execution_paper_mode_places_paper_order(settings):
    pf = Portfolio(settings)
    eng = ExecutionEngine.create(settings, pf)  # settings default = paper
    assert eng.paper is not None
    assert eng.live is None
