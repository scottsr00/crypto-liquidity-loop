"""Chronological candle simulation with explicitly approximate execution."""

import hashlib
import math
import statistics
from dataclasses import replace
from datetime import datetime, timezone

from .accounting import apply_fill, mark
from .domain import DAY, D, equity, floor_step, signal, validate_candles
from .storage import encode

# Fixed before inspecting results. No automatic parameter selection is performed.
RESEARCH_PERIODS = (
    ("training", "2019-01-01", "2022-01-01"),
    ("validation", "2022-01-01", "2024-01-01"),
    ("held_out", "2024-01-01", "2026-01-01"),
)


def utc_day(value):
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


class History:
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.product_id = client.product_id
        self.inputs = {}

    def get(self, start, end, step=DAY):
        candles = self.store.candles(start, end, step, product_id=self.product_id)
        try:
            validate_candles(candles, start, end, step)
        except ValueError:
            candles = self.client.candles(start, end, step)
            self.store.save_candles(candles, step, product_id=self.product_id)
            validate_candles(candles, start, end, step)
        content = [{k: getattr(c, k) for k in c.__dataclass_fields__} for c in candles]
        self.inputs[(start, end, step)] = {
            "start": start,
            "end": end,
            "step": step,
            "count": len(candles),
            "sha256": hashlib.sha256(encode(content).encode()).hexdigest(),
            "product_id": self.product_id,
        }
        return candles


def run_backtest(
    config, product, history, start, end, *, maker_fills=True, label="custom", equity_features=None
):
    product_id = getattr(history, "product_id", "BTC-USD")
    asset = product_id.split("-")[0]
    if start % DAY or end % DAY or start >= end:
        raise ValueError("Backtest requires increasing UTC dates")
    needed = max(config.sma_days, config.entry_days + 1, config.exit_days + 1)
    if hasattr(history, "inputs"):
        history.inputs = {}
    daily = history.get(start - needed * DAY, end)
    validate_candles(daily, start - needed * DAY, end)
    state = {
        "cash": str(config.initial_cash),
        "btc": "0",
        "fees": "0",
        "peak": str(config.initial_cash),
    }
    benchmark = dict(state)
    curve, fills, decisions = [], [], []
    halted, missed, orders, cash_days, turnover = False, 0, 0, 0, D(0)
    liquidation_pending = False
    observed_drawdown = D(0)
    spread, slip = config.spread_bps / 20000, config.slippage_bps / 10000
    warmup_index = needed

    def fill(side, qty, price, maker, when):
        nonlocal turnover
        fee = apply_fill(state, side, qty, price, config.maker_fee if maker else config.taker_fee)
        turnover += qty * price
        fills.append(
            {
                "timestamp": when,
                "product_id": product_id,
                "side": side,
                "quantity": str(qty),
                "price": str(price),
                "fee": str(fee),
                "liquidity": "maker" if maker else "taker",
            }
        )

    def sell_market(price, when):
        nonlocal orders
        price = floor_step(price * (1 - spread) * (1 - slip), product.quote_increment)
        qty = floor_step(D(state["btc"]), product.base_increment)
        if product.valid(qty, price):
            fill("SELL", qty, price, False, when)
            orders += 1

    for index in range(warmup_index, len(daily)):
        today = daily[index]
        if liquidation_pending:
            # A prior closing valuation cannot use tomorrow's execution price.
            next_minute = history.get(today.start, today.start + 60, 60)[0]
            sell_market(next_minute.open, next_minute.start)
            liquidation_pending = D(state["btc"]) > 0
        decision = signal(daily[:index], config)
        if equity_features is not None:
            decision = equity_features.filter(today.start, decision)
        decisions.append(decision)
        action = decision["action"]
        execution_needed = index == warmup_index or (
            not halted
            and (
                (action == "enter" and D(state["btc"]) == 0)
                or (action == "exit" and D(state["btc"]) > 0)
            )
        )
        if execution_needed:
            # One minute of publication/processing latency; fills can only occur afterward.
            window = (
                config.entry_window_seconds if action == "enter" else config.exit_window_seconds
            )
            stop = today.start + max(180, ((window + 119) // 60) * 60)
            minutes = history.get(today.start, min(stop, today.start + DAY), 60)
            price_at_evaluation = minutes[1].open
            if index == warmup_index:
                price = price_at_evaluation * (1 + spread) * (1 + slip)
                qty = product.buy_quantity(config.initial_cash, price, config.taker_fee)
                apply_fill(benchmark, "BUY", qty, price, config.taker_fee)
            if not halted and action == "enter" and D(state["btc"]) == 0:
                price = floor_step(price_at_evaluation * (1 - spread), product.quote_increment)
                remaining = product.buy_quantity(D(state["cash"]), price, config.maker_fee)
                initial = remaining
                if product.valid(remaining, price):
                    orders += 1
                    for minute in minutes[1:]:
                        if minute.start + 60 > today.start + config.entry_window_seconds:
                            break
                        if maker_fills and minute.low < price:
                            qty = floor_step(
                                min(remaining, minute.volume * config.volume_participation),
                                product.base_increment,
                            )
                            if qty > 0:
                                fill("BUY", qty, price, True, minute.start + 60)
                                remaining -= qty
                        value, dd = mark(state, minute.close * (1 - spread))
                        observed_drawdown = max(observed_drawdown, dd)
                        if dd >= config.drawdown_limit:
                            halted = True
                            # Use the next observed open, never the earlier triggering close.
                            next_bar = next(
                                (m for m in minutes if m.start == minute.start + 60), None
                            )
                            if next_bar:
                                sell_market(next_bar.open, next_bar.start)
                            else:
                                next_start = minute.start + 60
                                next_bar = history.get(next_start, next_start + 60, 60)[0]
                                sell_market(next_bar.open, next_bar.start)
                            break
                        if remaining == 0:
                            break
                    if remaining == initial:
                        missed += 1
                else:
                    missed += 1
            elif not halted and action == "exit" and D(state["btc"]) > 0:
                step = product.quote_increment
                price = floor_step(price_at_evaluation * (1 + spread), step) + step
                orders += 1
                deadline = today.start + 60 + config.exit_window_seconds
                for minute in minutes[1:]:
                    if minute.start + 60 > deadline:
                        break
                    if maker_fills and minute.high > price:
                        qty = floor_step(
                            min(D(state["btc"]), minute.volume * config.volume_participation),
                            product.base_increment,
                        )
                        if qty > 0:
                            fill("SELL", qty, price, True, minute.start + 60)
                    if D(state["btc"]) == 0:
                        break
                if D(state["btc"]) > 0:
                    next_bar = next((m for m in minutes if m.start >= deadline), None)
                    if next_bar is None:
                        next_bar = history.get(
                            ((deadline + 59) // 60) * 60, ((deadline + 59) // 60) * 60 + 60, 60
                        )[0]
                    sell_market(next_bar.open, next_bar.start)
        bid = today.close * (1 - spread)
        value, dd = mark(state, bid)
        observed_drawdown = max(observed_drawdown, dd)
        if dd >= config.drawdown_limit and not halted:
            halted = True
            liquidation_pending = True
        bv, bdd = mark(benchmark, bid)
        cash_days += int(D(state["btc"]) == 0)
        curve.append(
            {
                "timestamp": today.start + DAY,
                "equity": str(value),
                "drawdown": str(dd),
                "benchmark": str(bv),
                "benchmark_drawdown": str(bdd),
            }
        )

    final_bid = daily[-1].close * (1 - spread)
    ending = equity(state, final_bid)
    benchmark_ending = equity(benchmark, final_bid)
    duration_days = (end - start) / DAY
    operating = config.monthly_operating_cost * D(str(duration_days)) / D("30.4375")
    returns = []
    previous = float(config.initial_cash)
    for row in curve:
        current = float(row["equity"])
        returns.append(current / previous - 1 if previous else 0)
        previous = current
    std = statistics.stdev(returns) if len(returns) > 1 else 0
    sharpe = statistics.mean(returns) / std * math.sqrt(365) if std else None
    liquidation = D(state["cash"]) + D(state["btc"]) * final_bid * (1 - slip) * (
        1 - config.taker_fee
    )
    benchmark_liquidation = D(benchmark["cash"]) + D(benchmark["btc"]) * final_bid * (1 - slip) * (
        1 - config.taker_fee
    )
    data_hash = hashlib.sha256(
        encode([{k: getattr(c, k) for k in c.__dataclass_fields__} for c in daily]).encode()
    ).hexdigest()
    return {
        "label": label,
        "mode": "backtest",
        "product_id": product_id,
        "asset": asset,
        "start": start,
        "end": end,
        "parameters": config.public(),
        "product": {k: str(getattr(product, k)) for k in product.__dataclass_fields__},
        "source": f"Coinbase Advanced Trade public REST {product_id}; daily signals and minute execution windows",
        "daily_data_sha256": data_hash,
        "data_inputs": list(getattr(history, "inputs", {}).values()),
        "equity_filter": (
            {
                **equity_features.manifest(start, end),
                "rule": "Allow breakout strategy only when prior 20-session equity return > 0; otherwise exit",
                "alignment": "US session usable next UTC midnight; fail on missing/stale features",
                "limitation": "Current adjusted history is not point-in-time vintage data",
            }
            if equity_features is not None
            else None
        ),
        "maker_fills_enabled": maker_fills,
        "assumptions": [
            "Execution is approximate: OHLC cannot prove queue priority or actual fills.",
            "One minute evaluation latency; limit fills require strict penetration and are capped by volume participation.",
            "Fixed assumed spread/slippage and fees; current product increments applied historically.",
            "Risk sampled at daily closes outside execution windows and minute closes during entry execution; this does not validate the five-second live halt.",
            "No intraday peak reconstruction, deposits, withdrawals, taxes, or automatic halt resume.",
            "Benchmark buys at the first evaluation with taker costs; both portfolios use bid valuation and equal ending liquidation costs.",
            "Operating costs charged to strategy; passive wealth also shown with the same operating charge for comparison.",
        ],
        "metrics": {
            "ending_wealth": str(ending),
            "benchmark_wealth": str(benchmark_ending),
            "liquidated_wealth": str(liquidation),
            "benchmark_liquidated_wealth": str(benchmark_liquidation),
            "total_return": str(ending / config.initial_cash - 1),
            "annualized_return": float(
                (ending / config.initial_cash) ** (D(365) / D(str(duration_days))) - 1
            )
            if duration_days >= 365 and ending > 0
            else None,
            "max_drawdown": str(observed_drawdown),
            "benchmark_max_drawdown": str(
                max((D(r["benchmark_drawdown"]) for r in curve), default=D(0))
            ),
            "daily_sharpe": sharpe,
            "sharpe_definition": "Sample standard deviation of daily simple returns; sqrt(365); zero risk-free rate",
            "time_in_cash": cash_days / len(curve),
            "orders": orders,
            "fills": len(fills),
            "missed_entries": missed,
            "fees": state["fees"],
            "turnover": str(turnover / config.initial_cash),
            "operating_cost": str(operating),
            "wealth_after_operating_cost": str(ending - operating),
            "benchmark_after_same_operating_cost": str(benchmark_ending - operating),
            "halted": halted,
            "residual_quantity": state["btc"],
            "residual_asset": asset,
            "residual_btc": state["btc"] if asset == "BTC" else None,
        },
        "curve": curve,
        "fills": fills,
        "decisions": decisions,
    }


def research_suite(config, product, history):
    reports = []

    def attempt(candidate, product, history, start, end, **kwargs):
        try:
            return run_backtest(candidate, product, history, start, end, **kwargs)
        except ValueError as exc:
            if "Incomplete candles" not in str(exc):
                raise
            product_id = getattr(history, "product_id", "BTC-USD")
            print(f"{kwargs['label']} incomplete: {exc}", flush=True)
            return {
                "label": kwargs["label"],
                "product_id": product_id,
                "asset": product_id.split("-")[0],
                "start": start,
                "end": end,
                "parameters": candidate.public(),
                "status": "incomplete",
                "error": str(exc),
                "maker_fills_enabled": kwargs.get("maker_fills", True),
            }

    sol = getattr(history, "product_id", "BTC-USD") == "SOL-USD"
    validation_start = "2022-03-01" if sol else "2022-01-01"
    coverage_notes = (
        [
            "SOL Coinbase history begins in June 2021. Training 2019–2021 and annual 2021 are unavailable with sufficient warmup.",
            "SOL validation and annual 2022 start March 1, 2022, allowing 250 days of warmup for the slowest variant. Compare held-out results across assets using identical 2024–2025 dates.",
        ]
        if sol
        else []
    )
    # Freeze each partition; held-out results never choose a parameter set.
    for label, start, end in RESEARCH_PERIODS:
        if sol and label == "training":
            print("SOL training skipped: unavailable pre-period history and warmup", flush=True)
            continue
        if label == "validation":
            start = validation_start
        print(f"Research period: {label} {start} to {end}", flush=True)
        reports.append(attempt(config, product, history, utc_day(start), utc_day(end), label=label))
    for entry, exit_, sma in ((40, 15, 150), (70, 25, 200), (100, 40, 250)):
        candidate = replace(config, entry_days=entry, exit_days=exit_, sma_days=sma)
        reports.append(
            attempt(
                candidate,
                product,
                history,
                utc_day(validation_start),
                utc_day("2024-01-01"),
                label=f"validation-neighborhood-{entry}-{exit_}-{sma}",
            )
        )
    for label, candidate, enabled in (
        ("validation-no-maker-fills", config, False),
        (
            "validation-higher-costs",
            replace(
                config, slippage_bps=D("25"), spread_bps=D("5"), volume_participation=D("0.005")
            ),
            True,
        ),
    ):
        reports.append(
            attempt(
                candidate,
                product,
                history,
                utc_day(validation_start),
                utc_day("2024-01-01"),
                maker_fills=enabled,
                label=label,
            )
        )
    for year in (2021, 2022, 2023):
        if sol and year == 2021:
            continue
        reports.append(
            attempt(
                config,
                product,
                history,
                utc_day(validation_start if sol and year == 2022 else f"{year}-01-01"),
                utc_day(f"{year + 1}-01-01"),
                label=f"walk-forward-fixed-{year}",
            )
        )
    for report in reports:
        report["coverage_notes"] = coverage_notes
    return reports
