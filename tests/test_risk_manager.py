import time

from app.config import BotMode, Settings, StrategyName
from app.models import (ClaudeReview, OrderIntent, RiskLevel, Side, Signal)
from app.portfolio import Portfolio
from app.risk_manager import KillSwitch, RiskManager
from tests.conftest import make_book


def _signal(strategy=StrategyName.near_certainty, size=10.0, price=0.99):
    return Signal(
        market_id="m1", token_id="t", strategy=strategy, side=Side.BUY,
        estimated_fair_price=0.997, best_bid=0.985, best_ask=0.99, edge=0.007,
        confidence=0.7, max_price=price, suggested_size_usd=size,
    )


def _intent(price=0.99, size=10.0, strategy=StrategyName.near_certainty):
    return OrderIntent(market_id="m1", token_id="t", side=Side.BUY, price=price,
                       size_usd=size, strategy=strategy)


def _ok_review():
    return ClaudeReview(allow_trade=True, risk_level=RiskLevel.low,
                        resolution_ambiguity=False, valid_json=True)


def _rm(settings):
    pf = Portfolio(settings)
    return RiskManager(settings, pf, KillSwitch()), pf


def test_happy_path_allows(settings):
    rm, _ = _rm(settings)
    book = make_book("t", 0.985, 0.99)
    d = rm.validate(_intent(), _signal(), book, _ok_review())
    assert d.allowed
    assert d.approved_size_usd is not None


def test_invalid_claude_json_blocks(settings):
    rm, _ = _rm(settings)
    bad = ClaudeReview(allow_trade=True, valid_json=False)
    d = rm.validate(_intent(), _signal(), make_book("t", 0.985, 0.99), bad)
    assert not d.allowed
    assert d.reason == "claude_invalid_json"


def test_claude_deny_blocks(settings):
    rm, _ = _rm(settings)
    deny = ClaudeReview(allow_trade=False, valid_json=True, resolution_ambiguity=False)
    d = rm.validate(_intent(), _signal(), make_book("t", 0.985, 0.99), deny)
    assert not d.allowed
    assert d.reason.startswith("claude_blocked")


def test_resolution_ambiguity_blocks(settings):
    rm, _ = _rm(settings)
    amb = ClaudeReview(allow_trade=True, valid_json=True, resolution_ambiguity=True)
    d = rm.validate(_intent(), _signal(), make_book("t", 0.985, 0.99), amb)
    assert not d.allowed
    assert d.reason == "resolution_ambiguity"


def test_stale_book_blocks(settings):
    rm, _ = _rm(settings)
    stale = make_book("t", 0.985, 0.99, ts=time.time() - 30)
    d = rm.validate(_intent(), _signal(), stale, _ok_review())
    assert not d.allowed
    assert d.reason == "stale_order_book"


def test_wide_spread_blocks(settings):
    rm, _ = _rm(settings)
    wide = make_book("t", 0.90, 0.99)  # spread 0.09 > max 0.03
    d = rm.validate(_intent(), _signal(), wide, _ok_review())
    assert not d.allowed
    assert d.reason.startswith("spread_too_wide")


def test_strategy_size_cap_blocks(settings):
    rm, _ = _rm(settings)
    # near-certainty cap is 1% of 1000 = $10; ask for $50
    big = _signal(size=50.0)
    d = rm.validate(_intent(size=50.0), big, make_book("t", 0.985, 0.99), _ok_review())
    assert not d.allowed
    assert d.reason.startswith("strategy_size_cap")


def test_account_exposure_trims_or_blocks(settings):
    rm, pf = _rm(settings)
    # Manually load near the account cap (25% of 1000 = $250).
    from app.models import Fill
    pf.apply_fill(Fill(client_order_id="x", token_id="other", side=Side.BUY,
                       price=0.50, size_shares=496), market_id="mX")  # $248
    d = rm.validate(_intent(size=10.0), _signal(size=10.0),
                    make_book("t", 0.985, 0.99), _ok_review())
    assert d.allowed
    assert d.approved_size_usd <= 2.0 + 1e-6  # trimmed to remaining $2 room


def test_daily_loss_trips_kill_switch(settings):
    rm, pf = _rm(settings)
    pf.realized_pnl_usd = -50.0  # 5% loss > 4% limit
    d = rm.validate(_intent(), _signal(), make_book("t", 0.985, 0.99), _ok_review())
    assert not d.allowed
    assert d.reason.startswith("daily_loss_limit")
    assert rm.kill.tripped


def test_kill_switch_blocks_new_entries(settings):
    rm, _ = _rm(settings)
    rm.kill.trip("manual")
    d = rm.validate(_intent(), _signal(), make_book("t", 0.985, 0.99), _ok_review())
    assert not d.allowed
    assert d.reason.startswith("kill_switch_tripped")


def test_paused_mode_blocks(settings):
    settings.bot_mode = BotMode.paused
    rm, _ = _rm(settings)
    d = rm.validate(_intent(), _signal(), make_book("t", 0.985, 0.99), _ok_review())
    assert not d.allowed
    assert d.reason == "bot_paused"


def test_reasoner_disabled_blocks_in_live_mode():
    s = Settings(_env_file=None, bot_mode=BotMode.tiny_live, reasoner_disabled=True,
                 bankroll_usd=1000.0)
    pf = Portfolio(s)
    rm = RiskManager(s, pf, KillSwitch())
    d = rm.validate(_intent(), _signal(), make_book("t", 0.985, 0.99), _ok_review())
    assert not d.allowed
    assert d.reason == "reasoner_disabled_in_live_mode"


def test_buy_price_chasing_blocked(settings):
    rm, _ = _rm(settings)
    # intent price far above best ask + max_spread
    d = rm.validate(_intent(price=0.99), _signal(),
                    make_book("t", 0.50, 0.55), _ok_review())
    assert not d.allowed
    assert d.reason == "buy_price_chasing"


def test_check_kill_switches_latency():
    s = Settings(_env_file=None, bankroll_usd=1000.0, max_api_latency_ms=100.0)
    pf = Portfolio(s)
    rm = RiskManager(s, pf, KillSwitch())
    rm.check_kill_switches(latency_ms=500.0)
    assert rm.kill.tripped
