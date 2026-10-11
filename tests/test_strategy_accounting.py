from dataclasses import replace

import pytest

from crypto_liquidity_loop.accounting import apply_fill, mark
from crypto_liquidity_loop.config import Config
from crypto_liquidity_loop.domain import DAY, Candle, D, Product, signal, validate_candles


def candles():
    return [Candle(i * DAY, D("100"), D("110"), D("90"), D("100"), D("10")) for i in range(200)]


def test_entry_excludes_signal_high_and_includes_signal_sma():
    data = candles()
    data[-1] = Candle(data[-1].start, D("100"), D("200"), D("90"), D("120"), D("10"))
    result = signal(data, Config())
    assert result["action"] == "enter"
    assert D(result["channel_high"]) == 110
    assert D(result["sma"]) == D("100.1")
    assert result["timestamp"] == 200 * DAY


def test_entry_equality_and_exit_equality_do_not_trade():
    data = candles()
    data[-1] = Candle(data[-1].start, D("100"), D("110"), D("90"), D("110"), D("10"))
    assert signal(data, Config())["action"] == "hold"
    data[-1] = Candle(data[-1].start, D("100"), D("110"), D("90"), D("90"), D("10"))
    assert signal(data, Config())["action"] == "hold"


def test_exit_excludes_signal_low():
    data = candles()
    data[-1] = Candle(data[-1].start, D("100"), D("110"), D("70"), D("80"), D("10"))
    assert signal(data, Config())["action"] == "exit"


def test_warmup_and_gap_rejection():
    data = candles()
    assert signal(data[:199], Config())["action"] == "warmup"
    with pytest.raises(ValueError, match="Incomplete"):
        signal(data[:80] + data[81:], replace(Config(), sma_days=150))
    with pytest.raises(ValueError):
        validate_candles(data + data[-1:], 0, 200 * DAY)


def test_fees_cash_and_btc_are_counted_once():
    state = {"cash": "500", "btc": "0", "fees": "0", "peak": "500"}
    assert apply_fill(state, "BUY", D("2"), D("100"), D(".005")) == 1
    assert state["cash"] == "299.000"
    assert D(state["btc"]) == 2
    assert mark(state, D("100"))[0] == 499
    assert apply_fill(state, "SELL", D("2"), D("110"), D(".009")) == D("1.980")
    assert D(state["cash"]) == D("517.020")
    assert D(state["btc"]) == 0
    assert D(state["fees"]) == D("2.980")


def test_sizing_reserves_fees_and_respects_steps():
    product = Product()
    quantity = product.buy_quantity(D("500"), D("12345.67"), D(".005"))
    assert quantity * D("12345.67") * D("1.005") <= 500
    assert (quantity + product.base_increment) * D("12345.67") * D("1.005") > 500


@pytest.mark.parametrize(
    "settings",
    [
        {"mode": "live"},
        {"poll_seconds": 6},
        {"maker_fee": D("NaN")},
        {"entry_days": 0},
        {"drawdown_limit": D("1")},
    ],
)
def test_invalid_or_live_configuration_fails(settings):
    with pytest.raises(ValueError):
        Config(**settings)
