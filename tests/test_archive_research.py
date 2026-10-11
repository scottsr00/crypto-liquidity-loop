from decimal import Decimal
from zipfile import ZipFile

import pytest

from crypto_liquidity_loop.archive_research import (
    MACROS,
    MARKETS,
    MarketFeatures,
    Observation,
    paired_test,
    read_series,
    run_archive_research,
)
from crypto_liquidity_loop.backtest import utc_day
from crypto_liquidity_loop.domain import DAY, Candle, D
from crypto_liquidity_loop.storage import Store

HEADER = "<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>\n"


def test_parser_and_untrusted_members(tmp_path):
    path = tmp_path / "market.zip"
    with ZipFile(path, "w") as z:
        z.writestr("../../outside.txt", HEADER + "^NDQ,D,20240104,000000,1,1,1,1,0,0\n")
        z.writestr("duplicates.txt", HEADER + "^NDQ,D,20240104,000000,1,1,1,1,0,0\n" * 2)
        z.writestr("nan.txt", HEADER + "^NDQ,D,20240104,000000,1,1,1,NaN,0,0\n")
    with ZipFile(path) as z:
        rows, digest = read_series(z, "../../outside.txt", "^NDQ")
        assert rows == [Observation("2024-01-04", D(1), D(0))]
        assert len(digest) == 64
        with pytest.raises(ValueError, match="Ticker"):
            read_series(z, "../../outside.txt", "^NDX")
        with pytest.raises(ValueError, match="Duplicate"):
            read_series(z, "duplicates.txt", "^NDQ")
        with pytest.raises(ValueError, match="numeric"):
            read_series(z, "nan.txt", "^NDQ")
    assert not (tmp_path.parent / "outside.txt").exists()


def test_market_alignment_and_yield_changes():
    rows = [
        Observation("2024-01-04", D(2), D(0)),
        Observation("2024-01-05", D(3), D(0)),
        Observation("2024-01-08", D(4), D(0)),
    ]
    feature = MarketFeatures("10YUSY.B", rows, "difference", {})
    assert feature.at(utc_day("2024-01-05"), 1) is None
    assert feature.at(utc_day("2024-01-06"), 1) == 1
    assert feature.at(utc_day("2024-01-08"), 1) == 1
    assert feature.at(utc_day("2024-01-14"), 1) is None
    prices = MarketFeatures("INDEX", rows, "return", {})
    assert prices.at(utc_day("2024-01-06"), 1) == 0.5
    with pytest.raises(ValueError, match="only supports"):
        feature.filter(utc_day("2024-01-06"), {"action": "enter"})


def test_archive_research_is_offline_and_macro_is_excluded(tmp_path):
    from datetime import datetime, timezone

    world, macro = tmp_path / "world.zip", tmp_path / "macro.zip"
    dates = [
        datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d")
        for t in range(utc_day("2023-10-01"), utc_day("2024-04-01"), DAY)
        if datetime.fromtimestamp(t, timezone.utc).weekday() < 5
    ]

    def text(ticker):
        return HEADER + "".join(
            f"{ticker},D,{day},000000,{100 + i},{100 + i},{100 + i},{100 + i},0,0\n"
            for i, day in enumerate(dates)
        )

    with ZipFile(world, "w") as z:
        for symbol, (suffix, _) in MARKETS.items():
            z.writestr("data/daily/world/" + suffix, text(symbol))
        for symbol in ("BTC", "ETH", "SOL"):
            z.writestr(
                f"data/daily/world/cryptocurrencies/{symbol.lower()}.v.txt", text(symbol + ".V")
            )
    with ZipFile(macro, "w") as z:
        for symbol in MACROS:
            z.writestr(f"data/daily/macro/us/{symbol.lower()}.txt", text(symbol))
    store = Store(tmp_path / "db.sqlite3")
    start, end = utc_day("2024-01-01"), utc_day("2024-03-01")
    candles = [
        Candle(t, D(100 + i * i), D(100 + i * i), D(100 + i * i), D(100 + i * i), D(0))
        for i, t in enumerate(range(start, end, DAY))
    ]
    store.save_candles(candles, DAY)
    report = run_archive_research(store, world, macro, start, end)
    assert len(report["tests"]) == 32  # 30 BTC tests, incomplete ETH and SOL
    assert all(r["status"] == "excluded_from_predictive_tests" for r in report["macro_inventory"])
    assert all(r.get("market") not in MACROS for r in report["tests"])
    assert report["tests"][0]["samples"] == len(candles) - 1
    assert report["tests"][1]["samples"] == len(candles) - 7
    assert store.candles(start, end, product_id="ETH-USD") == []
    assert store.state() is None
    assert report["archives"][0]["series_count"] == 8


def test_future_rows_do_not_change_predictor():
    from datetime import datetime, timezone

    start, end = utc_day("2024-02-01"), utc_day("2024-04-01")
    rows = [
        Observation(
            datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), Decimal(100 + i), D(0)
        )
        for i, t in enumerate(range(start - 60 * DAY, end, DAY))
    ]
    past = MarketFeatures("INDEX", rows, "return", {})
    future = MarketFeatures(
        "INDEX", rows + [Observation("2024-04-01", D(90000), D(0))], "return", {}
    )
    candles = [
        Candle(t, D(100 + i * i), D(100 + i * i), D(100 + i * i), D(100 + i * i), D(0))
        for i, t in enumerate(range(start, end, DAY))
    ]
    assert paired_test(past, candles, 20, 7) == paired_test(future, candles, 20, 7)
