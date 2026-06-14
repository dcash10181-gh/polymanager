from app.config import Settings, StrategyName
from app.claude_reasoner import ClaudeReasoner
from app.models import Side, Signal
from tests.conftest import make_binary_market, make_book


def _signal():
    return Signal(market_id="m1", token_id="YES", strategy=StrategyName.near_certainty,
                  side=Side.BUY, estimated_fair_price=0.99, edge=0.01, max_price=0.99,
                  suggested_size_usd=10.0)


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResp:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [FakeBlock(text)]
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, resp=None, raise_exc=None):
        self._resp = resp
        self._raise = raise_exc

    def create(self, **kwargs):
        if self._raise:
            raise self._raise
        return self._resp


class FakeClient:
    def __init__(self, resp=None, raise_exc=None):
        self.messages = FakeMessages(resp, raise_exc)


def _settings_live():
    return Settings(_env_file=None, reasoner_disabled=False, anthropic_api_key="x")


def test_disabled_reasoner_auto_allows():
    s = Settings(_env_file=None, reasoner_disabled=True)
    r = ClaudeReasoner(s)
    review = r.review(make_binary_market(), _signal(), make_book("YES", 0.98, 0.99))
    assert review.allow_trade and review.valid_json


def test_valid_json_response_parsed():
    good = ('{"allow_trade": true, "risk_level": "low", '
            '"resolution_ambiguity": false, "main_risks": ["thin_book"], '
            '"confidence_adjustment": -0.2, "comment": "ok"}')
    r = ClaudeReasoner(_settings_live(), client=FakeClient(FakeResp(good)))
    review = r.review(make_binary_market(), _signal())
    assert review.allow_trade
    assert review.valid_json
    assert review.confidence_adjustment == -0.2
    assert "thin_book" in review.main_risks


def test_invalid_json_blocks():
    r = ClaudeReasoner(_settings_live(), client=FakeClient(FakeResp("not json at all")))
    review = r.review(make_binary_market(), _signal())
    assert not review.allow_trade
    assert not review.valid_json


def test_refusal_blocks():
    good = ('{"allow_trade": true, "risk_level": "low", "resolution_ambiguity": '
            'false, "main_risks": [], "confidence_adjustment": 0.0, "comment": ""}')
    r = ClaudeReasoner(_settings_live(),
                       client=FakeClient(FakeResp(good, stop_reason="refusal")))
    review = r.review(make_binary_market(), _signal())
    assert not review.allow_trade
    assert not review.valid_json


def test_api_error_blocks():
    r = ClaudeReasoner(_settings_live(),
                       client=FakeClient(raise_exc=RuntimeError("boom")))
    review = r.review(make_binary_market(), _signal())
    assert not review.allow_trade
    assert not review.valid_json
    assert "reasoner_unavailable" in review.main_risks
