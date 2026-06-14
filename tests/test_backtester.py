from app.backtester import Backtester
from app.config import Settings, StrategyName
from tests.conftest import make_binary_market, make_book


def _settings():
    return Settings(_env_file=None, bankroll_usd=1000.0, reasoner_disabled=True,
                    max_spread=0.03)


def test_backtest_momentum_takes_profit():
    s = _settings()
    bt = Backtester(s, enabled=[StrategyName.momentum_lag], fill_ratio=1.0)
    m = make_binary_market()
    frames = [
        # price low, external fair high -> buy YES (~0.57)
        {"timestamp": 1000.0, "market": m,
         "books": {"YES": make_book("YES", 0.55, 0.57, ask_size=5000, ts=1000.0)},
         "external": {"fair_price:YES": 0.62}},
        # price rallies past the take-profit -> exit manager flattens for a gain
        {"timestamp": 1001.0, "market": m,
         "books": {"YES": make_book("YES", 0.61, 0.63, bid_size=5000, ts=1001.0)},
         "external": {}},
    ]
    res = bt.run(frames)
    assert res.signals >= 1
    assert res.orders >= 2                       # entry + take-profit exit
    assert abs(bt.portfolio.get_position("YES").shares) < 1e-9  # flat after exit
    assert res.realized_pnl_usd > 0              # bought ~0.57, sold ~0.61


def test_backtest_arbitrage_places_both_legs():
    s = _settings()
    bt = Backtester(s, enabled=[StrategyName.arbitrage], fill_ratio=1.0)
    m = make_binary_market()
    frames = [{
        "timestamp": 1000.0, "market": m,
        "books": {
            "YES": make_book("YES", 0.55, 0.57, ask_size=5000, ts=1000.0),
            "NO": make_book("NO", 0.38, 0.40, ask_size=5000, ts=1000.0),
        },
        "external": {},
    }]
    res = bt.run(frames)
    assert res.by_strategy.get("arbitrage", 0) >= 1
    # both legs should have acquired inventory
    assert bt.portfolio.get_position("YES").shares > 0
    assert bt.portfolio.get_position("NO").shares > 0


def test_backtest_runs_clean_with_no_signals():
    s = _settings()
    bt = Backtester(s)
    m = make_binary_market()
    frames = [{"timestamp": 1.0, "market": m,
               "books": {"YES": make_book("YES", 0.50, 0.51, ts=1.0)},
               "external": {}}]
    res = bt.run(frames)
    assert res.frames == 1
    assert res.orders == 0
