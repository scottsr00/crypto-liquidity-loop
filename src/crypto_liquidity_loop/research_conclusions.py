"""Explain stored results without selecting parameters or rerunning experiments."""

import json
from datetime import datetime, timezone

from .domain import D


def latest_reports(rows):
    seen, result = set(), []
    for row in rows:
        report = json.loads(row["body"])
        product_id = report.get("product_id", "BTC-USD")
        asset = product_id.split("-")[0]
        parameters = {k: v for k, v in report["parameters"].items() if k != "database"}
        key = (
            product_id,
            report["label"],
            report["start"],
            report["end"],
            json.dumps(parameters, sort_keys=True),
            report.get("maker_fills_enabled", True),
        )
        if key in seen:
            continue
        seen.add(key)
        if report.get("status") == "incomplete":
            continue
        metrics = report["metrics"]
        initial = D(report["parameters"]["initial_cash"])
        gap = D(metrics["ending_wealth"]) - D(metrics["benchmark_wealth"])
        label = report["label"]
        role = (
            "Held-out test"
            if label == "held_out"
            else "Training"
            if label == "training"
            else "Validation"
            if label == "validation"
            else "Parameter variant · validation only"
            if "neighborhood" in label
            else "Execution sensitivity · validation only"
            if label.startswith("validation-")
            else "Independent annual run"
            if label.startswith("walk-forward")
            else "Custom period"
        )
        result.append(
            {
                **row,
                "body": report,
                "role": role,
                "asset": asset,
                "product_id": product_id,
                "gap": gap,
                "result": f"Beat {asset}"
                if gap > 0
                else f"Lagged {asset}"
                if gap < 0
                else f"Matched {asset}",
                "direction": "positive" if gap > 0 else "negative" if gap < 0 else "muted",
                "no_fills": metrics["fills"] == 0,
                "start_date": day(report["start"]),
                "end_date": day(report["end"]),
                "strategy_return": D(metrics["ending_wealth"]) / initial - 1,
                "benchmark_return": D(metrics["benchmark_wealth"]) / initial - 1,
                "settings_label": " / ".join(
                    str(report["parameters"][k]) for k in ("entry_days", "exit_days", "sma_days")
                ),
            }
        )
    return result


def day(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")


def trade_cycles(fills):
    result, cycle, quantity = [], None, D(0)
    for fill in sorted(fills, key=lambda f: f["timestamp"]):
        qty, price, fee = (D(fill[k]) for k in ("quantity", "price", "fee"))
        if fill["side"] == "BUY":
            if cycle is None:
                cycle = {
                    "entry": day(fill["timestamp"]),
                    "buy_notional": D(0),
                    "sell_notional": D(0),
                    "fees": D(0),
                    "buy_quantity": D(0),
                }
            quantity += qty
            cycle["buy_quantity"] += qty
            cycle["buy_notional"] += qty * price
        else:
            if cycle is None or qty > quantity:
                raise ValueError("Backtest sell exceeds the reconstructed position")
            quantity -= qty
            cycle["sell_notional"] += qty * price
        cycle["fees"] += fee
        if quantity == 0:
            cycle.update(
                exit=day(fill["timestamp"]),
                entry_price=cycle["buy_notional"] / cycle["buy_quantity"],
                exit_price=cycle["sell_notional"] / cycle["buy_quantity"],
                pnl=cycle["sell_notional"] - cycle["buy_notional"] - cycle["fees"],
            )
            result.append(cycle)
            cycle = None
    return result


def main_conclusion(reports):
    held = next((r for r in reports if r["body"]["label"] == "held_out"), None)
    if held is None:
        return None
    report, metrics = held["body"], held["body"]["metrics"]
    initial = D(report["parameters"]["initial_cash"])
    fees = D(metrics["fees"])
    net = D(metrics["ending_wealth"]) - initial
    cycles = trade_cycles(report["fills"])
    variants = [r for r in reports if "neighborhood" in r["body"]["label"]]
    # Compare periods with identical signal parameters and execution settings.
    settings = {k: v for k, v in report["parameters"].items() if k != "database"}
    baseline = [
        r
        for label in ("training", "validation", "held_out")
        for r in reports
        if r["body"]["label"] == label
        and {k: v for k, v in r["body"]["parameters"].items() if k != "database"} == settings
    ]
    return {
        "held": held,
        "baseline": baseline,
        "net_gain": net,
        "gross_pnl": net + fees,
        "benchmark_gain": D(metrics["benchmark_wealth"]) - initial,
        "shortfall": max(D(0), -held["gap"]),
        "fees": fees,
        "cycles": cycles,
        "completed_trades": len(cycles),
        "wins": sum(c["pnl"] > 0 for c in cycles),
        "losses": sum(c["pnl"] < 0 for c in cycles),
        "cash_fraction": metrics["time_in_cash"],
        "missed_entries": metrics["missed_entries"],
        "halted": metrics["halted"],
        "variants": variants,
        "variant_wins": sum(r["gap"] > 0 for r in variants),
        "drawdown_difference": D(metrics["benchmark_max_drawdown"]) - D(metrics["max_drawdown"]),
    }
