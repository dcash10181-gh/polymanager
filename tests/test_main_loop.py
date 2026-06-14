"""End-to-end smoke test of the main loop with a fake gateway (no network)."""

from app.config import BotMode, Settings
from app.main import TradingApp
from tests.conftest import make_binary_market, make_book


class FakeGateway:
    def __init__(self, market, books):
        self.market = market
        self.books = books

    def list_markets(self, filters=None):
        return [self.market]

    def get_orderbooks(self, token_ids):
        return {t: self.books[t] for t in token_ids if t in self.books}

    def get_positions(self, wallet=None):
        return []

    def close(self):
        pass


def test_main_loop_paper_opens_position():
    s = Settings(_env_file=None, bot_mode=BotMode.paper, reasoner_disabled=True,
                 bankroll_usd=1000.0, db_path=":memory:", min_liquidity_usd=1000.0,
                 min_24h_volume_usd=100.0, max_spread=0.03)
    market = make_binary_market()
    books = {
        "YES": make_book("YES", 0.55, 0.57, ask_size=5000),
        "NO": make_book("NO", 0.38, 0.40, ask_size=5000),
    }
    app = TradingApp(s, gateway=FakeGateway(market, books))
    # Supply external fair value so the momentum strategy has a basis to fire.
    app.external_provider = lambda m: {"fair_price:YES": 0.62}

    app.run_once()

    # Either momentum (YES) or arbitrage (YES+NO) should have transacted.
    total_shares = sum(abs(p.shares) for p in app.portfolio.open_positions())
    assert total_shares > 0
    app.shutdown()


def test_main_loop_research_places_no_orders():
    s = Settings(_env_file=None, bot_mode=BotMode.research, reasoner_disabled=True,
                 bankroll_usd=1000.0, db_path=":memory:", min_liquidity_usd=1000.0,
                 min_24h_volume_usd=100.0)
    market = make_binary_market()
    books = {"YES": make_book("YES", 0.55, 0.57, ask_size=5000),
             "NO": make_book("NO", 0.38, 0.40, ask_size=5000)}
    app = TradingApp(s, gateway=FakeGateway(market, books))
    app.external_provider = lambda m: {"fair_price:YES": 0.62}
    app.run_once()
    # research mode never places orders -> no positions
    assert app.portfolio.open_positions() == []
    app.shutdown()
