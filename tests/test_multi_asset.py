import sqlite3
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from test_research_conclusions import report, row

from crypto_liquidity_loop import backtest
from crypto_liquidity_loop.coinbase import CoinbasePublic
from crypto_liquidity_loop.dashboard import create_app
from crypto_liquidity_loop.domain import DAY, Candle, D, Product
from crypto_liquidity_loop.research_conclusions import latest_reports
from crypto_liquidity_loop.storage import SCHEMA, Store, encode


def test_migration_preserves_btc_and_isolates_asset_candles(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    candle = Candle(0, D(100), D(110), D(90), D(100), D(1))
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO candles VALUES(?,?,?,?,?)", (DAY, 0, encode(asdict(candle)), "legacy", 1)
        )
    store = Store(path)
    for asset, price in [("ETH-USD", 200), ("SOL-USD", 300)]:
        store.save_candles(
            [Candle(0, D(price), D(price), D(price), D(price), D(1))], DAY, product_id=asset
        )
    assert store.candles(0, DAY)[0].close == 100
    assert store.candles(0, DAY, product_id="ETH-USD")[0].close == 200
    assert store.candles(0, DAY, product_id="SOL-USD")[0].close == 300
    assert Store(path).candles(0, DAY)[0].close == 100


def test_public_history_requests_selected_asset():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "candles": [
                    {
                        "start": "0",
                        "open": "100",
                        "high": "110",
                        "low": "90",
                        "close": "100",
                        "volume": "1",
                    }
                ]
            },
        )

    for asset in ("ETH-USD", "SOL-USD"):
        client = CoinbasePublic(httpx.MockTransport(handler), product_id=asset)
        assert len(client.candles(0, DAY)) == 1
        assert paths[-1].endswith(f"/{asset}/candles")
        client.close()


def test_sol_suite_has_warmup_and_same_held_out_period(paper, monkeypatch):
    _, config, _ = paper
    calls = []

    def fake(config, product, history, start, end, **kwargs):
        calls.append((config, start, end, kwargs["label"]))
        return {}

    monkeypatch.setattr(backtest, "run_backtest", fake)
    reports = backtest.research_suite(config, Product(), SimpleNamespace(product_id="SOL-USD"))
    assert len(reports) == 9
    assert all(r["coverage_notes"] for r in reports)
    assert not any(label == "training" or label.endswith("2021") for _, _, _, label in calls)
    for _, start, end, label in calls:
        if label == "held_out":
            assert (start, end) == (backtest.utc_day("2024-01-01"), backtest.utc_day("2026-01-01"))
        elif "validation" in label or label.endswith("2022"):
            assert start == backtest.utc_day("2022-03-01")


def test_reports_and_dashboard_keep_assets_separate(paper, monkeypatch):
    store, config, _ = paper
    monkeypatch.delenv("LOOP_DASHBOARD_TOKEN", raising=False)
    btc = report(config)
    eth = deepcopy(btc)
    eth.update(product_id="ETH-USD", asset="ETH")
    eth["metrics"]["ending_wealth"] = "700"
    reports = latest_reports([row(eth, "eth"), row(btc, "btc")])
    assert len(reports) == 2
    assert reports[0]["result"] == "Beat ETH"
    with store.transaction() as db:
        for identifier, body in [("btc", btc), ("eth", eth)]:
            db.execute("INSERT INTO reports VALUES(?,?,?)", (identifier, 1, encode(body)))
    client = TestClient(create_app(store, config))
    assert "Main strategy lagged BTC" in client.get("/").text
    page = client.get("/?research_asset=ETH").text
    assert "Main strategy beat ETH" in page
    assert "Main strategy lagged BTC" not in page
    assert "Not run yet" in page
    assert client.get("/?research_asset=DOGE").status_code == 400


def test_incomplete_suite_period_is_recorded_and_other_tests_continue(paper, monkeypatch):
    _, config, _ = paper

    def fake(config, product, history, start, end, **kwargs):
        if kwargs["label"] == "validation":
            raise ValueError("Incomplete candles: missing required minute")
        return {"label": kwargs["label"]}

    monkeypatch.setattr(backtest, "run_backtest", fake)
    reports = backtest.research_suite(config, Product(), SimpleNamespace(product_id="SOL-USD"))
    rejected = reports[0]
    assert rejected["status"] == "incomplete"
    assert "metrics" not in rejected
    assert latest_reports([row(rejected)]) == []
    assert reports[1]["label"] == "held_out"
    assert len(reports) == 9
