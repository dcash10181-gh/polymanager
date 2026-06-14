import time

from app.config import StrategyName
from app.exit_manager import ExitManager
from app.models import OrderIntent, Position, Side
from tests.conftest import make_book


def _intent(tp=0.62, stop=0.55, time_stop=None, token="t"):
    return OrderIntent(market_id="m1", token_id=token, side=Side.BUY, price=0.57,
                       size_usd=10.0, strategy=StrategyName.momentum_lag,
                       take_profit_price=tp, stop_price=stop,
                       time_stop_seconds=time_stop)


def _long(shares=100.0, avg=0.57, opened_at=None):
    return Position(token_id="t", market_id="m1", strategy=StrategyName.momentum_lag,
                    shares=shares, avg_price=avg,
                    opened_at=opened_at if opened_at is not None else time.time())


def test_register_skips_targetless_intents(settings):
    em = ExitManager(settings)
    em.register(OrderIntent(market_id="m", token_id="t", side=Side.BUY, price=0.5,
                            size_usd=10, strategy=StrategyName.arbitrage))
    assert "t" not in em.plans


def test_take_profit_exit_on_long(settings):
    em = ExitManager(settings)
    em.register(_intent(tp=0.62, stop=0.55))
    book = make_book("t", bid=0.63, ask=0.64)  # mid 0.635 >= tp
    intent = em.evaluate(_long(), book)
    assert intent is not None
    assert intent.side is Side.SELL
    assert intent.price == 0.63  # sells into the bid


def test_stop_loss_exit_on_long(settings):
    em = ExitManager(settings)
    em.register(_intent(tp=0.62, stop=0.55))
    book = make_book("t", bid=0.54, ask=0.545)  # mid ~0.5425 <= stop
    intent = em.evaluate(_long(), book)
    assert intent is not None
    assert intent.side is Side.SELL


def test_no_exit_when_between_targets(settings):
    em = ExitManager(settings)
    em.register(_intent(tp=0.62, stop=0.55))
    book = make_book("t", bid=0.57, ask=0.58)  # mid 0.575, between stop and tp
    assert em.evaluate(_long(), book) is None


def test_time_stop_exit(settings):
    em = ExitManager(settings)
    em.register(_intent(tp=0.99, stop=0.01, time_stop=300))
    book = make_book("t", bid=0.57, ask=0.58)
    old = _long(opened_at=time.time() - 600)  # older than the 300s time stop
    intent = em.evaluate(old, book)
    assert intent is not None


def test_short_take_profit(settings):
    em = ExitManager(settings)
    # short entry: tp below, stop above
    em.register(OrderIntent(market_id="m1", token_id="t", side=Side.SELL, price=0.57,
                            size_usd=10.0, strategy=StrategyName.momentum_lag,
                            take_profit_price=0.50, stop_price=0.60))
    book = make_book("t", bid=0.48, ask=0.49)  # mid 0.485 <= tp 0.50
    pos = Position(token_id="t", market_id="m1", strategy=StrategyName.momentum_lag,
                   shares=-100.0, avg_price=0.57)
    intent = em.evaluate(pos, book)
    assert intent is not None
    assert intent.side is Side.BUY  # buy back to cover


def test_stale_book_no_exit(settings):
    em = ExitManager(settings)
    em.register(_intent())
    stale = make_book("t", bid=0.63, ask=0.64, ts=time.time() - 30)
    assert em.evaluate(_long(), stale) is None


def test_forget_on_closed_position(settings):
    em = ExitManager(settings)
    em.register(_intent())
    flat = Position(token_id="t", shares=0.0)
    assert em.evaluate(flat, make_book("t", 0.63, 0.64)) is None
    assert "t" not in em.plans
