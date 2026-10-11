from copy import deepcopy

from fastapi.testclient import TestClient

from crypto_liquidity_loop.dashboard import create_app
from crypto_liquidity_loop.domain import D
from crypto_liquidity_loop.research_conclusions import (
    latest_reports,
    main_conclusion,
    trade_cycles,
)
from crypto_liquidity_loop.storage import encode


def report(config, label="held_out", wealth="508.95", benchmark="600"):
    return {
        "label": label,
        "start": 1704067200,
        "end": 1767225600,
        "parameters": config.public(),
        "maker_fills_enabled": True,
        "metrics": {
            "ending_wealth": wealth,
            "benchmark_wealth": benchmark,
            "fees": "1.05",
            "fills": 2,
            "time_in_cash": 0.6,
            "missed_entries": 1,
            "halted": False,
            "max_drawdown": ".1",
            "benchmark_max_drawdown": ".2",
            "operating_cost": "20",
            "wealth_after_operating_cost": str(D(wealth) - 20),
            "benchmark_after_same_operating_cost": str(D(benchmark) - 20),
        },
        "fills": [
            {"timestamp": 1704758520, "side": "BUY", "quantity": "1", "price": "100", "fee": ".5"},
            {
                "timestamp": 1705968120,
                "side": "SELL",
                "quantity": "1",
                "price": "110",
                "fee": ".55",
            },
        ],
    }


def row(body, identifier="new", timestamp=2):
    return {"id": identifier, "timestamp": timestamp, "body": encode(body)}


def test_latest_run_deduplication_retains_different_experiments(paper):
    _, config, _ = paper
    held = report(config)
    other = deepcopy(held)
    other["parameters"]["entry_days"] = 70
    reports = latest_reports([row(held), row(other, "variant"), row(held, "old", 1)])
    assert [r["id"] for r in reports] == ["new", "variant"]


def test_attribution_reconciles_gain_fees_and_completed_trade(paper):
    _, config, _ = paper
    conclusions = main_conclusion(latest_reports([row(report(config))]))
    assert conclusions["shortfall"] == D("91.05")
    assert conclusions["net_gain"] == D("8.95")
    assert conclusions["gross_pnl"] == 10
    assert conclusions["fees"] == D("1.05")
    assert sum(c["pnl"] for c in conclusions["cycles"]) == conclusions["net_gain"]
    assert conclusions["completed_trades"] == 1
    assert conclusions["wins"] == 1


def test_partial_fills_group_into_one_position():
    fills = [
        {"timestamp": 1, "side": "BUY", "quantity": ".4", "price": "100", "fee": ".2"},
        {"timestamp": 2, "side": "BUY", "quantity": ".6", "price": "110", "fee": ".33"},
        {"timestamp": 3, "side": "SELL", "quantity": ".5", "price": "120", "fee": ".3"},
        {"timestamp": 4, "side": "SELL", "quantity": ".5", "price": "130", "fee": ".325"},
    ]
    assert trade_cycles(fills[:-1]) == []
    cycles = trade_cycles(fills)
    assert len(cycles) == 1
    assert cycles[0]["entry_price"] == 106
    assert cycles[0]["exit_price"] == 125
    assert cycles[0]["fees"] == D("1.155")
    assert cycles[0]["pnl"] == D("17.845")


def test_zero_fill_success_is_not_execution_validation(paper):
    _, config, _ = paper
    zero = report(config, "validation-no-maker-fills", "500", "400")
    zero["metrics"]["fills"] = 0
    zero["fills"] = []
    result = latest_reports([row(zero)])[0]
    assert result["result"] == "Beat BTC"
    assert result["no_fills"]
    assert result["role"] == "Execution sensitivity · validation only"
    assert main_conclusion([result]) is None


def test_dashboard_distinguishes_main_periods_and_validation_variants(paper, monkeypatch):
    store, config, _ = paper
    monkeypatch.delenv("LOOP_DASHBOARD_TOKEN", raising=False)
    data = [
        report(config),
        report(config, "training", "550", "700"),
        report(config, "validation", "550", "450"),
        report(config, "validation-neighborhood-70-25-200", "560", "450"),
    ]
    data[-1]["parameters"]["entry_days"] = 70
    with store.transaction() as db:
        for index, body in enumerate(data):
            db.execute("INSERT INTO reports VALUES(?,?,?)", (str(index), index, encode(body)))
        db.execute("INSERT INTO reports VALUES(?,?,?)", ("duplicate", -1, encode(data[0])))
    client = TestClient(create_app(store, config))
    response = client.get("/")
    assert response.status_code == 200
    assert "Main strategy lagged BTC in the held-out test" in response.text
    assert "1 of 1 parameter variants beat BTC" in response.text
    assert "validation data only" in response.text
    assert "$91.05" in response.text
    assert "Gross P/L uses recorded fill quantities" in response.text
    assert "No" in response.text
    assert "/reports/duplicate" not in response.text
