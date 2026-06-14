from app.config import StrategyName
from app.models import Side
from app.strategies.arbitrage import ArbitrageStrategy
from app.strategies.base import StrategyContext
from app.strategies.cheap_tail import CheapTailStrategy
from app.strategies.momentum_lag import MomentumLagStrategy
from app.strategies.near_certainty import NearCertaintyStrategy
from tests.conftest import make_binary_market, make_book


def ctx(market, book, books=None, external=None):
    return StrategyContext(market=market, book=book, books=books or {},
                           external=external or {})


def test_near_certainty_fires_with_external_prob(settings):
    m = make_binary_market()
    book = make_book("YES", bid=0.985, ask=0.99, ask_size=5000)
    sigs = NearCertaintyStrategy(settings).evaluate(
        ctx(m, book, external={"true_prob:YES": 0.997}))
    assert len(sigs) == 1
    s = sigs[0]
    assert s.strategy is StrategyName.near_certainty
    assert s.side is Side.BUY
    assert s.edge > 0


def test_near_certainty_dormant_without_external(settings):
    m = make_binary_market()
    book = make_book("YES", bid=0.985, ask=0.99)
    sigs = NearCertaintyStrategy(settings).evaluate(ctx(m, book))
    assert sigs == []


def test_near_certainty_rejects_insufficient_prob(settings):
    m = make_binary_market()
    book = make_book("YES", bid=0.985, ask=0.99)
    # true_prob below ask + buffer (0.99 + 0.005 = 0.995)
    sigs = NearCertaintyStrategy(settings).evaluate(
        ctx(m, book, external={"true_prob:YES": 0.992}))
    assert sigs == []


def test_momentum_fires_with_fair_price(settings):
    m = make_binary_market()
    book = make_book("YES", bid=0.55, ask=0.57, ask_size=2000)
    sigs = MomentumLagStrategy(settings).evaluate(
        ctx(m, book, external={"fair_price:YES": 0.62}))
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert abs(sigs[0].edge - 0.05) < 1e-9


def test_momentum_sell_when_fair_below_bid(settings):
    m = make_binary_market()
    book = make_book("YES", bid=0.55, ask=0.57, bid_size=2000)
    sigs = MomentumLagStrategy(settings).evaluate(
        ctx(m, book, external={"fair_price:YES": 0.48}))
    assert len(sigs) == 1
    assert sigs[0].side is Side.SELL


def test_momentum_dormant_without_external(settings):
    m = make_binary_market()
    book = make_book("YES", bid=0.55, ask=0.57)
    assert MomentumLagStrategy(settings).evaluate(ctx(m, book)) == []


def test_cheap_tail_requires_prob_and_catalyst(settings):
    m = make_binary_market()
    book = make_book("YES", bid=0.02, ask=0.03, ask_size=5000)
    # prob >= ask*1.75 = 0.0525 AND catalyst true
    sigs = CheapTailStrategy(settings).evaluate(
        ctx(m, book, external={"true_prob:YES": 0.06, "catalyst:YES": True}))
    assert len(sigs) == 1
    assert sigs[0].strategy is StrategyName.cheap_tail
    # no catalyst -> dormant
    assert CheapTailStrategy(settings).evaluate(
        ctx(m, book, external={"true_prob:YES": 0.06})) == []


def test_arbitrage_fires_when_asks_sum_below_one(settings):
    m = make_binary_market()
    yes = make_book("YES", bid=0.55, ask=0.57, ask_size=5000)
    no = make_book("NO", bid=0.38, ask=0.40, ask_size=5000)
    books = {"YES": yes, "NO": no}
    sigs = ArbitrageStrategy(settings).evaluate(ctx(m, yes, books=books))
    assert len(sigs) == 1
    s = sigs[0]
    assert s.strategy is StrategyName.arbitrage
    assert abs(s.edge - 0.03) < 1e-9
    assert len(s.legs) == 2


def test_arbitrage_dormant_when_no_edge(settings):
    m = make_binary_market()
    yes = make_book("YES", bid=0.58, ask=0.60)
    no = make_book("NO", bid=0.41, ask=0.43)  # sum asks = 1.03 > 1
    books = {"YES": yes, "NO": no}
    assert ArbitrageStrategy(settings).evaluate(ctx(m, yes, books=books)) == []
