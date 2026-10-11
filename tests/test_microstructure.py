import csv
from copy import deepcopy

import pytest

from crypto_liquidity_loop.backtest import utc_day
from crypto_liquidity_loop.microstructure import (
    COLUMNS,
    Rules,
    detect,
    event_study,
    load_csv,
    parse_klines,
)


def observations(count=30):
    start = utc_day("2023-11-01")
    result = []
    for i in range(count):
        t = start + (i + 1) * 300
        row = dict.fromkeys(COLUMNS)
        row.update(
            asset="BTC",
            timestamp=t,
            available_at=t + 1,
            spot_venue="test-spot",
            perp_venue="test-perp",
            book_venue="test-spot",
            open=100.0,
            high=100.1,
            low=99.9,
            close=100.0,
            spot_buy_quote=130.0,
            spot_sell_quote=70.0,
            perp_buy_quote=150.0,
            perp_sell_quote=50.0,
            oi_base=100.0 + i,
            funding_rate=0.0004,
            funding_interval_hours=8.0,
            bid_depth_quote=3000.0,
            ask_depth_quote=1000.0,
            spread_bps=2.0,
            book_band_bps=25.0,
            oi_timestamp=t,
            funding_timestamp=t,
            book_timestamp=t,
            spot_complete=True,
            perp_complete=True,
            book_complete=True,
        )
        result.append(row)
    return result


def save(path, rows):
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {k: str(v).lower() if isinstance(v, bool) else v for k, v in row.items()}
            )


def test_cvd_absorption_and_perp_rally():
    data = observations()
    signals = detect(data, 12)["signals"]
    assert signals["spot_absorption"] is True
    assert signals["perp_only_rally"] is False
    # A level shift before the observation window must not reset inferred delta.
    data[-1]["close"] = 100.5
    for row in data:
        row["spot_buy_quote"] = row["spot_sell_quote"] = 100.0
    signals = detect(data, 29)["signals"]
    assert signals["perp_only_rally"] is True
    assert signals["spot_absorption"] is False


def test_oi_setup_precedes_opposite_trigger():
    data = observations(14)
    data[-1]["close"] = 99.7
    result = detect(data, 13)
    assert result["signals"]["crowded_longs"] is True
    assert result["signals"]["long_flush_trigger"] is True
    assert result["signals"]["short_squeeze_trigger"] is False
    # A quote-notional OI jump solely on the trigger bar cannot create prior buildup.
    for row in data[:-1]:
        row["oi_base"] = 100.0
    data[-1]["oi_base"] = 10000.0
    assert detect(data, 13)["signals"]["crowded_longs"] is False
    # Short crowding uses negative funding and aggressive sell flow.
    data = observations(14)
    for row in data:
        row["perp_buy_quote"], row["perp_sell_quote"] = 50.0, 150.0
        row["funding_rate"] = -0.0004
    data[-1]["close"] = 100.3
    assert detect(data, 13)["signals"]["short_squeeze_trigger"] is True


def test_depth_requires_persistence_band_and_flow_confirmation():
    data = observations(13)
    data[-1]["ask_depth_quote"] = 100.0
    assert detect(data, 12)["signals"]["ask_liquidity_squeeze"] is False
    for row in data[-3:]:
        row["ask_depth_quote"] = 100.0
    assert detect(data, 12)["signals"]["ask_liquidity_squeeze"] is True
    data[-1]["book_band_bps"] = 50.0
    assert detect(data, 12)["signals"]["ask_liquidity_squeeze"] is None
    data[-1]["book_band_bps"] = 25.0
    data[-1]["book_complete"] = False
    assert detect(data, 12)["signals"]["ask_liquidity_squeeze"] is None


def test_missing_stale_delayed_and_gap_inputs_fail_closed():
    data = observations(13)
    for row in data:
        row["perp_complete"] = False
        row["oi_base"] = None
        row["book_complete"] = False
    signals = detect(data, 12)["signals"]
    assert signals["spot_absorption"] is True
    assert signals["perp_only_rally"] is None
    assert signals["crowded_longs"] is None
    assert signals["ask_liquidity_squeeze"] is None
    delayed = deepcopy(data)
    delayed[5]["available_at"] = delayed[-1]["available_at"] + 1
    assert detect(delayed, 12)["status"] == "unavailable_or_stale"
    assert detect(data[:5] + data[6:], 11)["status"] == "warmup"
    data = observations(14)
    assert detect(data[:5] + data[6:], 12)["status"] == "gap"
    data = observations(13)
    data[5]["oi_timestamp"] -= 3600
    assert detect(data, 12)["signals"]["crowded_longs"] is None
    data[5]["spot_venue"] = "different"
    assert detect(data, 12)["signals"]["spot_absorption"] is None


def test_csv_validation_and_event_execution_after_availability(tmp_path):
    data = observations(100)
    path = tmp_path / "input.csv"
    save(path, data)
    rows, digest = load_csv(path)
    report = event_study(rows, digest, data[0]["timestamp"], data[-1]["timestamp"] + 300)
    assert report["coverage"][1]["status"] == "incomplete"
    test = next(
        t
        for t in report["tests"]
        if t["asset"] == "BTC" and t["signal"] == "spot_absorption" and t["horizon_minutes"] == 15
    )
    assert test["events"] > 0
    assert test["overlapping_events_skipped"] > 0
    # A huge price exactly at signal time is not the execution price.
    data = observations(20)
    data[13]["open"] = 1000.0
    report = event_study(data, "x", data[12]["timestamp"], data[13]["timestamp"])
    assert all(t["events"] == 0 for t in report["tests"])
    data[0]["available_at"] = data[0]["timestamp"] - 1
    save(path, data)
    with pytest.raises(ValueError, match="Availability"):
        load_csv(path)
    data = observations(13)
    data[0]["oi_timestamp"] = data[0]["timestamp"] + 1
    save(path, data)
    with pytest.raises(ValueError, match="future"):
        load_csv(path)
    save(path, observations(1) * 2)
    with pytest.raises(ValueError, match="Duplicate"):
        load_csv(path)


@pytest.mark.parametrize("unit", [1000, 1_000_000])
def test_binance_kline_units_and_exact_delta(unit):
    start = utc_day("2025-01-01")
    line = f"{start * unit},100,101,99,100,10,{(start + 300) * unit - 1},1000,10,6,600,0\n"
    result = parse_klines(line.encode(), "ETH", "spot")[start + 300]
    assert result["spot_buy_quote"] == 600
    assert result["spot_sell_quote"] == 400
    assert result["available_at"] == start + 301
    assert result["oi_base"] == ""
    assert result["book_complete"] == "false"
    with pytest.raises(ValueError, match="Duplicate"):
        parse_klines((line * 2).encode(), "ETH", "spot")


def test_funding_interval_normalization_and_future_invariance():
    data = observations(14)
    for row in data:
        row["funding_rate"] = 0.0002
        row["funding_interval_hours"] = 4.0
    before = detect(data, 13)
    assert before["signals"]["crowded_longs"] is True
    future = observations(15)[-1]
    future["close"] = 99999.0
    assert detect(data + [future], 13) == before
    assert before["features"]["funding_per_8h"] == 0.0004
    assert Rules().step == 300


def test_event_study_skips_signal_close_and_prices_before_availability():
    from crypto_liquidity_loop.config import Config

    data = observations(20)
    for row in data[13:]:
        row["spot_complete"] = False
    data[13]["open"] = 1000000.0  # Earlier full-interval open is still too early.
    data[17]["open"] = 110.0
    report = event_study(
        data, "x", data[12]["timestamp"], data[-1]["timestamp"] + 300, costs=Config()
    )
    row = next(
        t
        for t in report["tests"]
        if t["asset"] == "BTC" and t["signal"] == "spot_absorption" and t["horizon_minutes"] == 15
    )
    assert row["events"] == 1
    assert row["mean_forward_spot_return"] == pytest.approx(0.1)
    assert row["illustrative_mean_long_return_after_costs"] < 0.1
    assert (
        report["coverage"][0]["status"] == "complete"
        or report["coverage"][0]["status"] == "partial"
    )
    assert report["illustrative_costs"]["taker_fee"] == "0.009"


def test_zero_volume_and_stale_funding_are_unknown():
    data = observations(14)
    for row in data:
        row["spot_buy_quote"] = row["spot_sell_quote"] = 0.0
        row["funding_timestamp"] -= 9 * 3600
    signals = detect(data, 13)["signals"]
    assert signals["spot_absorption"] is None
    assert signals["crowded_longs"] is None
