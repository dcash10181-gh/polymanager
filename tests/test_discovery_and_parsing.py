import time

from app.config import Settings
from app.market_discovery import MarketDiscovery
from app.polymarket_client import parse_book, parse_market
from tests.conftest import make_binary_market


def test_parse_market_decodes_string_arrays():
    raw = {
        "id": "123", "question": "Q?", "conditionId": "0xabc",
        "clobTokenIds": "[\"111\", \"222\"]",
        "outcomes": "[\"Yes\", \"No\"]",
        "outcomePrices": "[\"0.6\", \"0.4\"]",
        "liquidity": "9000", "volume24hr": "5000", "spread": "0.01",
        "endDate": "2026-12-31T00:00:00Z", "active": True, "closed": False,
    }
    m = parse_market(raw)
    assert m.token_ids == ["111", "222"]
    assert m.is_binary
    assert m.liquidity_usd == 9000.0
    assert m.volume_24h_usd == 5000.0
    assert m.tokens[0].outcome == "Yes"
    assert m.end_timestamp is not None


def test_parse_book_handles_string_levels():
    raw = {
        "asset_id": "111",
        "bids": [{"price": "0.55", "size": "100"}],
        "asks": [{"price": "0.57", "size": "200"}],
        "timestamp": "1700000000000",
    }
    ob = parse_book(raw, "111")
    assert ob.best_bid == 0.55
    assert ob.best_ask == 0.57
    assert ob.bids[0].size == 100


class FakeGateway:
    def __init__(self, markets):
        self._markets = markets

    def list_markets(self, filters=None):
        return self._markets


def test_discovery_filters_low_liquidity():
    s = Settings(_env_file=None, min_liquidity_usd=10_000, min_24h_volume_usd=100)
    good = make_binary_market("good")
    good.liquidity_usd = 50_000
    bad = make_binary_market("bad")
    bad.liquidity_usd = 500  # below threshold
    disc = MarketDiscovery(FakeGateway([good, bad]), s)
    kept = disc.refresh()
    ids = [m.id for m in kept]
    assert "good" in ids
    assert "bad" not in ids


def test_discovery_rejects_imminent_resolution():
    s = Settings(_env_file=None, min_liquidity_usd=1000, min_24h_volume_usd=100)
    m = make_binary_market("soon")
    m.end_timestamp = time.time() + 30  # < 60s
    disc = MarketDiscovery(FakeGateway([m]), s)
    ok, reason = disc.passes_filters(m)
    assert not ok
    assert reason == "resolving_imminently"


def test_discovery_flags_ambiguous_wording():
    s = Settings(_env_file=None, min_liquidity_usd=1000, min_24h_volume_usd=100)
    m = make_binary_market("amb")
    m.description = "Resolves at the discretion of the moderators."
    disc = MarketDiscovery(FakeGateway([m]), s)
    ok, reason = disc.passes_filters(m)
    assert not ok
    assert reason == "ambiguous_wording_hint"
