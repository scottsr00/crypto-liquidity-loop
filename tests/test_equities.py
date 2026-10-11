from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from crypto_liquidity_loop.backtest import run_backtest, utc_day
from crypto_liquidity_loop.config import Config
from crypto_liquidity_loop.domain import DAY, Candle, D, Product
from crypto_liquidity_loop.equities import (
    AlphaVantage,
    EquityDay,
    EquityFeatures,
    correlation_report,
    load_equities,
    save_equities,
)
from crypto_liquidity_loop.storage import Store


def test_alpha_adjusted_history_and_cache(tmp_path):
    def handler(request):
        assert request.url.params["function"] == "TIME_SERIES_DAILY_ADJUSTED"
        assert request.url.params["outputsize"] == "full"
        return httpx.Response(
            200,
            json={
                "Meta Data": {"2. Symbol": "SPY", "5. Time Zone": "US/Eastern"},
                "Time Series (Daily)": {
                    "2024-01-05": {"4. close": "200", "5. adjusted close": "100"},
                    "2024-01-04": {"5. adjusted close": "99"},
                },
            },
        )

    client = AlphaVantage("secret", httpx.MockTransport(handler))
    try:
        rows = client.daily("spy")
    finally:
        client.close()
    assert rows[-1].adjusted_close == Decimal(100)
    store = Store(tmp_path / "data.sqlite3")
    save_equities(store, "SPY", rows)
    save_equities(store, "SPY", rows)
    assert load_equities(store, "SPY") == rows
    save_equities(store, "SPY", [EquityDay("2024-01-05", D(101))])
    assert store.rows("events")[0]["kind"] == "equity_revision"
    assert Store(store.path).candles(0, DAY) == []


@pytest.mark.parametrize(
    "payload", [{"Note": "secret"}, {"Information": "secret"}, {"Error Message": "secret"}, []]
)
def test_api_failures_never_expose_credentials(payload):
    client = AlphaVantage(
        "secret", httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    try:
        with pytest.raises(ValueError) as error:
            client.daily("SPY")
        assert "secret" not in str(error.value)
    finally:
        client.close()


def test_http_failure_redacts_request_url():
    client = AlphaVantage("secret", httpx.MockTransport(lambda request: httpx.Response(403)))
    try:
        with pytest.raises(ValueError) as error:
            client.daily("SPY")
        assert "secret" not in str(error.value)
        assert error.value.__suppress_context__
    finally:
        client.close()


def test_alignment_weekends_staleness_and_no_future_leakage():
    rows = [
        EquityDay("2024-01-04", D(100)),
        EquityDay("2024-01-05", D(110)),
        EquityDay("2024-01-08", D(50)),
    ]
    features = EquityFeatures("SPY", rows)
    assert features.at(utc_day("2024-01-05"), 1) is None
    assert features.at(utc_day("2024-01-06"), 1) == 0.1
    assert features.at(utc_day("2024-01-08"), 1) == 0.1
    assert features.at(utc_day("2024-01-09"), 1) < 0
    assert features.at(utc_day("2024-01-14"), 1) is None
    outage = EquityFeatures("SPY", [rows[0], EquityDay("2024-01-12", D(200))])
    assert outage.at(utc_day("2024-01-13"), 1) is None


def test_correlation_uses_future_opens_inside_partition(tmp_path):
    from datetime import datetime, timezone

    start, end = utc_day("2024-02-01"), utc_day("2024-04-01")
    store = Store(tmp_path / "data.sqlite3")
    rows = []
    for t in range(start - 60 * DAY, end, DAY):
        date = datetime.fromtimestamp(t, timezone.utc)
        if date.weekday() < 5:
            rows.append(EquityDay(date.strftime("%Y-%m-%d"), D(100 + len(rows))))
    save_equities(store, "SPY", rows)
    candles = [
        Candle(t, D(100 + i * i), D(102 + i * i), D(99 + i * i), D(101 + i * i), D(10))
        for i, t in enumerate(range(start, end, DAY))
    ]
    store.save_candles(candles, DAY)
    report = correlation_report(store, start, end, ["SPY"], ["BTC-USD"])
    assert len(report["tests"]) == 6
    assert all(r["status"] == "complete" for r in report["tests"])
    assert report["tests"][0]["samples"] == len(candles) - 1
    assert report["tests"][1]["samples"] == len(candles) - 7
    missing = correlation_report(store, start, end, ["QQQ"], ["BTC-USD"])
    assert all(r["status"] == "incomplete" for r in missing["tests"])
    assert all(r["pearson"] is None for r in missing["tests"])
    incomplete = correlation_report(store, start, end, ["SPY"], ["ETH-USD"])
    assert incomplete["tests"][0]["status"] == "incomplete"


def test_fixed_filter_uses_existing_cost_engine_and_fails_missing_data():
    from test_data_backtest import SyntheticHistory

    start, end = utc_day("2024-02-01"), utc_day("2024-02-04")
    from datetime import datetime, timezone

    rows = []
    for t in range(start - 50 * DAY, end, DAY):
        date = datetime.fromtimestamp(t, timezone.utc)
        if date.weekday() < 5:
            rows.append(EquityDay(date.strftime("%Y-%m-%d"), D(1000 - len(rows))))
    features = EquityFeatures("SPY", rows)
    config = replace(Config(), entry_days=2, exit_days=2, sma_days=3)
    base = run_backtest(config, Product(), SyntheticHistory(start), start, end)
    report = run_backtest(
        config, Product(), SyntheticHistory(start), start, end, equity_features=features
    )
    assert base["metrics"]["fills"] > 0
    assert report["metrics"]["fills"] == 0
    assert report["metrics"]["benchmark_wealth"] == base["metrics"]["benchmark_wealth"]
    assert report["equity_filter"]["symbol"] == "SPY"
    with pytest.raises(ValueError, match="Incomplete equity history"):
        run_backtest(
            config,
            Product(),
            SyntheticHistory(start),
            start,
            end,
            equity_features=EquityFeatures("SPY", []),
        )
