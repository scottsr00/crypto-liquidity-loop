import pytest

from crypto_liquidity_loop.config import Config
from crypto_liquidity_loop.domain import D, Product, Quote
from crypto_liquidity_loop.execution import PaperEngine
from crypto_liquidity_loop.storage import Store


@pytest.fixture
def paper(tmp_path):
    config = Config(database=str(tmp_path / "paper.sqlite3"))
    store = Store(config.database)
    store.initialize(config)
    engine = PaperEngine(store, config, Product())
    assert engine.reconcile(0)
    return store, config, engine


def quote(now, bid="100", ask="101", size="100"):
    return Quote(now, D(bid), D(ask), D(size), D(size))


def enter(engine, now=86400):
    engine.tick(quote(now), now, {"action": "enter", "timestamp": now})
