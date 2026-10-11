from dataclasses import replace

import httpx
import pytest

from crypto_liquidity_loop.backtest import run_backtest, utc_day
from crypto_liquidity_loop.coinbase import CoinbasePublic, advance_sequence
from crypto_liquidity_loop.config import Config
from crypto_liquidity_loop.domain import DAY, Candle, D, Product


def test_candle_pagination_and_duplicate_rejection():
    requests = []

    def handler(request):
        start, end = int(request.url.params["start"]), int(request.url.params["end"])
        requests.append((start, end))
        rows = [
            {
                "start": str(t),
                "open": "100",
                "high": "110",
                "low": "90",
                "close": "100",
                "volume": "1",
            }
            for t in range(start, end, DAY)
        ]
        return httpx.Response(200, json={"candles": list(reversed(rows))})

    client = CoinbasePublic(httpx.MockTransport(handler))
    try:
        assert len(client.candles(0, 400 * DAY)) == 400
        assert len(requests) == 2
    finally:
        client.close()

    def duplicates(request):
        row = {
            "start": "0",
            "open": "100",
            "high": "110",
            "low": "90",
            "close": "100",
            "volume": "1",
        }
        return httpx.Response(200, json={"candles": [row, row]})

    client = CoinbasePublic(httpx.MockTransport(duplicates))
    with pytest.raises(ValueError, match="duplicate"):
        client.candles(0, DAY)
    client.close()


def test_websocket_sequence_includes_subscription_acknowledgments():
    # Captured channel ordering from the actual public Coinbase connection.
    sequence = None
    for channel, number in (
        ("subscriptions", 0),
        ("l2_data", 1),
        ("l2_data", 2),
        ("subscriptions", 3),
        ("l2_data", 4),
        ("heartbeats", 5),
    ):
        sequence = advance_sequence(sequence, {"channel": channel, "sequence_num": number})
    assert sequence == 5
    with pytest.raises(ValueError, match="sequence gap"):
        advance_sequence(sequence, {"channel": "l2_data", "sequence_num": 7})


class SyntheticHistory:
    def __init__(self, start):
        self.start = start

    def get(self, start, end, step=DAY):
        result = []
        for t in range(start, end, step):
            if step == 60:
                c = Candle(t, D("200"), D("202"), D("190"), D("200"), D("100"))
            elif t == self.start - DAY:
                c = Candle(t, D("100"), D("210"), D("99"), D("200"), D("100"))
            elif t >= self.start:
                c = Candle(t, D("200"), D("220"), D("180"), D("205"), D("100"))
            else:
                c = Candle(t, D("100"), D("110"), D("90"), D("100"), D("100"))
            result.append(c)
        return result


def test_backtest_uses_post_signal_minutes_costs_and_matched_benchmark():
    start, end = utc_day("2024-01-01"), utc_day("2024-01-04")
    config = replace(Config(), entry_days=2, exit_days=2, sma_days=3)
    report = run_backtest(config, Product(), SyntheticHistory(start), start, end)
    assert report["metrics"]["fills"] > 0
    assert D(report["metrics"]["fees"]) > 0
    assert all(f["timestamp"] >= start + 120 for f in report["fills"])
    assert D(report["metrics"]["benchmark_wealth"]) > 0
    assert report["metrics"]["wealth_after_operating_cost"] != report["metrics"]["ending_wealth"]
    assert len(report["curve"]) == 3
    assert any("approximate" in a for a in report["assumptions"])


def test_no_fill_scenario_is_reported_not_execution_validation():
    start, end = utc_day("2024-01-01"), utc_day("2024-01-04")
    config = replace(Config(), entry_days=2, exit_days=2, sma_days=3)
    report = run_backtest(config, Product(), SyntheticHistory(start), start, end, maker_fills=False)
    assert report["metrics"]["fills"] == 0
    assert report["metrics"]["missed_entries"] == 1
    assert D(report["metrics"]["ending_wealth"]) == 500


def test_end_of_period_halt_does_not_trade_using_future_data():
    start, end = utc_day("2024-01-01"), utc_day("2024-01-02")

    class CrashHistory(SyntheticHistory):
        def get(self, start_, end_, step=DAY):
            assert end_ <= end
            data = super().get(start_, end_, step)
            if step == DAY:
                return [
                    Candle(c.start, D("200"), D("220"), D("15"), D("20"), D("100"))
                    if c.start >= start
                    else c
                    for c in data
                ]
            return data

    config = replace(Config(), entry_days=2, exit_days=2, sma_days=3)
    report = run_backtest(config, Product(), CrashHistory(start), start, end)
    assert report["metrics"]["halted"]
    assert D(report["metrics"]["residual_btc"]) > 0
    assert all(f["timestamp"] < end for f in report["fills"])
    assert D(report["metrics"]["max_drawdown"]) >= D(".70")
