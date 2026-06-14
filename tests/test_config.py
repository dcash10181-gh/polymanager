import pytest
from pydantic import ValidationError

from app.config import BotMode, Settings, StrategyName


def test_default_mode_is_paper():
    s = Settings(_env_file=None)
    assert s.bot_mode is BotMode.paper
    assert s.bot_mode.is_paper
    assert not s.bot_mode.places_real_orders


def test_mode_flags():
    assert BotMode.live.places_real_orders
    assert BotMode.tiny_live.places_real_orders
    assert BotMode.shadow.needs_credentials
    assert not BotMode.paper.needs_credentials


def test_usd_and_strategy_caps():
    s = Settings(_env_file=None, bankroll_usd=2000.0,
                 max_near_certainty_position_pct=0.01)
    assert s.usd(0.25) == 500.0
    assert s.strategy_max_usd(StrategyName.near_certainty) == 20.0


def test_invalid_exposure_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_account_exposure_pct=1.5)


def test_invalid_near_certainty_band_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, nc_enter_price_min=0.99, nc_enter_price_max=0.98)
