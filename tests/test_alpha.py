import time

from app.alpha.base import CompositeProvider, ExternalSignalProvider
from app.alpha.crypto_threshold import CryptoThresholdProvider, parse_question
from app.alpha.price_feed import PriceFeed
from app.alpha.pricing import normal_cdf, prob_above
from app.config import Settings, StrategyName
from app.strategies.base import StrategyContext
from app.strategies.momentum_lag import MomentumLagStrategy
from app.strategies.near_certainty import NearCertaintyStrategy
from tests.conftest import make_binary_market, make_book


# --- pricing ---------------------------------------------------------------
def test_normal_cdf_known_values():
    assert abs(normal_cdf(0.0) - 0.5) < 1e-9
    assert normal_cdf(5) > 0.999
    assert normal_cdf(-5) < 0.001


def test_prob_above_monotonic_and_bounds():
    # deep in the money -> ~1, deep out -> ~0, at strike -> ~0.5
    assert prob_above(200_000, 100_000, 0.6, 0.05) > 0.95
    assert prob_above(50_000, 100_000, 0.6, 0.05) < 0.05
    at = prob_above(100_000, 100_000, 0.6, 0.05)
    assert 0.4 < at < 0.5  # slightly below 0.5 from the -0.5*sigma^2 term


def test_prob_above_at_expiry_is_step():
    assert prob_above(101, 100, 0.6, 0.0) == 1.0
    assert prob_above(99, 100, 0.6, 0.0) == 0.0


def test_prob_above_invalid_inputs():
    assert prob_above(0, 100, 0.6, 0.1) is None
    assert prob_above(100, 0, 0.6, 0.1) is None
    assert prob_above(100, 100, 0.0, 0.1) is None


# --- parsing ---------------------------------------------------------------
def test_parse_question_above_variants():
    assert parse_question("Will Bitcoin be above $100,000 on Dec 31?") == ("BTC", "above", 100_000)
    assert parse_question("ETH above $4k by March?") == ("ETH", "above", 4_000)
    assert parse_question("Will Solana reach $300?") == ("SOL", "above", 300)


def test_parse_question_below():
    sym, direction, strike = parse_question("Will Bitcoin dip below $80,000?")
    assert sym == "BTC" and direction == "below" and strike == 80_000


def test_parse_question_non_crypto_returns_none():
    assert parse_question("Will the Lakers win the title?") is None
    assert parse_question("Bitcoin market with no price") is None


# --- price feed (offline) --------------------------------------------------
class _FakeResp:
    def __init__(self, amount):
        self._amount = amount

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": {"amount": self._amount, "base": "BTC", "currency": "USD"}}


class _FakeClient:
    def __init__(self, amount="95000.00"):
        self.amount = amount
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        return _FakeResp(self.amount)


def test_price_feed_parses_and_caches():
    fake = _FakeClient("95000.50")
    feed = PriceFeed(client=fake, ttl_seconds=60)
    assert feed.spot("BTC") == 95000.50
    feed.spot("BTC")  # cached -> no new HTTP call
    assert fake.calls == 1


def test_price_feed_serves_cache_on_error():
    class Boom(_FakeClient):
        def get(self, url, **kw):
            raise RuntimeError("network down")
    feed = PriceFeed(client=Boom(), ttl_seconds=0.0)
    assert feed.spot("BTC") is None  # no cache, error -> None


# --- provider end-to-end ---------------------------------------------------
class _StubFeed:
    def __init__(self, price):
        self.price = price

    def spot(self, symbol):
        return self.price


def _market(question, mid="m1"):
    m = make_binary_market(mid)
    m.question = question
    m.end_timestamp = time.time() + 5 * 86_400  # 5 days out
    return m


def test_crypto_provider_emits_fair_prices():
    s = Settings(_env_file=None)
    prov = CryptoThresholdProvider(s, _StubFeed(120_000))  # spot well above strike
    out = prov.signals(_market("Will Bitcoin be above $100,000 in 5 days?"))
    yes = "YES"
    assert out[f"fair_price:{yes}"] > 0.6      # likely YES
    assert out[f"true_prob:{yes}"] == out[f"fair_price:{yes}"]
    assert out["catalyst:YES"] is True         # within 7-day horizon
    # NO is the complement
    assert abs(out["fair_price:NO"] - (1 - out["fair_price:YES"])) < 1e-9


def test_crypto_provider_below_direction():
    s = Settings(_env_file=None)
    prov = CryptoThresholdProvider(s, _StubFeed(70_000))  # below strike
    out = prov.signals(_market("Will Bitcoin dip below $100,000 in 5 days?"))
    # spot far below 100k and asking 'below' -> YES very likely
    assert out["fair_price:YES"] > 0.9


def test_crypto_provider_ignores_non_crypto():
    s = Settings(_env_file=None)
    prov = CryptoThresholdProvider(s, _StubFeed(120_000))
    assert prov.signals(_market("Will it rain tomorrow?")) == {}


def test_alpha_drives_momentum_signal():
    """The provider's fair price should let momentum fire on a lagging book."""
    s = Settings(_env_file=None)
    market = _market("Will Bitcoin be above $100,000 in 5 days?")
    prov = CryptoThresholdProvider(s, _StubFeed(130_000))  # ~very likely YES
    external = prov.signals(market)
    # book lagging at 0.85/0.86 while fair ~0.97
    book = make_book("YES", 0.85, 0.86, ask_size=5000)
    sigs = MomentumLagStrategy(s).evaluate(
        StrategyContext(market=market, book=book, external=external))
    assert len(sigs) == 1
    assert sigs[0].strategy is StrategyName.momentum_lag


def test_composite_merges_and_survives_failure():
    class Good(ExternalSignalProvider):
        def signals(self, market):
            return {"true_prob:YES": 0.9}

    class Bad(ExternalSignalProvider):
        def signals(self, market):
            raise RuntimeError("boom")

    comp = CompositeProvider([Bad(), Good()])
    out = comp(make_binary_market())
    assert out == {"true_prob:YES": 0.9}
