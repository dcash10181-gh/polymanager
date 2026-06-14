from app.config import StrategyName
from app.models import OrderIntent, Side
from app.paper_broker import PaperBroker
from app.portfolio import Portfolio
from tests.conftest import make_book


def _intent(side=Side.BUY, price=0.69, size_usd=10.0):
    return OrderIntent(market_id="m", token_id="t", side=side, price=price,
                       size_usd=size_usd, strategy=StrategyName.momentum_lag)


def test_marketable_buy_fills_against_ask(settings):
    pf = Portfolio(settings)
    broker = PaperBroker(settings, pf, fill_ratio=1.0)
    book = make_book("t", bid=0.67, ask=0.68, ask_size=1000)
    order = broker.place_order(_intent(price=0.69), book)  # limit above ask -> fills at 0.68
    pos = pf.get_position("t")
    assert pos.shares > 0
    assert abs(pos.avg_price - 0.68) < 1e-9
    assert order.status.value == "filled"


def test_buy_limit_below_ask_does_not_fill(settings):
    pf = Portfolio(settings)
    broker = PaperBroker(settings, pf, fill_ratio=1.0)
    book = make_book("t", bid=0.67, ask=0.70)
    broker.place_order(_intent(price=0.68), book)  # below ask -> rests, no fill
    assert pf.get_position("t").shares == 0
    assert len(broker.open_orders) == 1


def test_fill_ratio_limits_size(settings):
    pf = Portfolio(settings)
    broker = PaperBroker(settings, pf, fill_ratio=0.5)
    # ask depth 10 shares; we want ~14.5 shares ($10 @ 0.69) but only 50% available
    book = make_book("t", bid=0.67, ask=0.69, ask_size=10)
    broker.place_order(_intent(price=0.69, size_usd=10.0), book)
    pos = pf.get_position("t")
    assert pos.shares <= 5.0 + 1e-9  # 50% of 10 resting shares


def test_resting_order_fills_on_later_book(settings):
    pf = Portfolio(settings)
    broker = PaperBroker(settings, pf, fill_ratio=1.0)
    broker.place_order(_intent(price=0.68), make_book("t", 0.67, 0.70))
    assert pf.get_position("t").shares == 0
    # ask later drops to 0.68 -> resting buy now fills
    broker.process_book(make_book("t", 0.66, 0.68, ask_size=1000))
    assert pf.get_position("t").shares > 0


def test_cancel_stale_orders(settings):
    pf = Portfolio(settings)
    broker = PaperBroker(settings, pf)
    broker.place_order(_intent(price=0.50), make_book("t", 0.40, 0.60))
    assert len(broker.open_orders) == 1
    n = broker.cancel_stale_orders(max_age_seconds=0.0)
    assert n == 1
    assert len(broker.open_orders) == 0


def test_sell_limit_fills_against_bid(settings):
    pf = Portfolio(settings)
    broker = PaperBroker(settings, pf, fill_ratio=1.0)
    book = make_book("t", bid=0.70, ask=0.72, bid_size=1000)
    broker.place_order(_intent(side=Side.SELL, price=0.69, size_usd=10.0), book)
    # sold shares -> negative position
    assert pf.get_position("t").shares < 0
