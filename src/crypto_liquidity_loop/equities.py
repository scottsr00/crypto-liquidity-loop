"""Adjusted US equity history and lagged, exploratory cross-market research."""

import hashlib
import json
import math
import os
import re
import statistics
import time
from bisect import bisect_right
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

import certifi
import httpx

from .backtest import utc_day
from .domain import DAY, validate_candles
from .storage import encode

DEFAULT_SYMBOLS = ("SPY", "QQQ", "IWM")


def equity_symbol(value):
    value = value.upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", value):
        raise ValueError("Expected a US equity/ETF ticker")
    return value


@dataclass(frozen=True)
class EquityDay:
    session_date: str
    adjusted_close: Decimal

    @property
    def available_at(self):
        # Conservative alignment for US listings, including early closes and DST.
        # This is a research convention, not a historical API publication timestamp.
        return utc_day(self.session_date) + DAY


class AlphaVantage:
    def __init__(self, api_key=None, transport=None):
        self.api_key = api_key or os.environ.get("ALPHAVANTAGE_API_KEY")
        if not self.api_key:
            raise ValueError("Set ALPHAVANTAGE_API_KEY in the environment before equity ingestion")
        self.client = httpx.Client(timeout=30, transport=transport, verify=certifi.where())

    def close(self):
        self.client.close()

    def daily(self, symbol):
        symbol = equity_symbol(symbol)
        try:
            response = self.client.get(
                "https://www.alphavantage.co/query",
                params={
                    "function": "TIME_SERIES_DAILY_ADJUSTED",
                    "symbol": symbol,
                    "outputsize": "full",
                    "apikey": self.api_key,
                },
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            # HTTP exceptions contain the URL/API key; never propagate them to logs.
            raise ValueError(
                "Alpha Vantage request failed; check connectivity and credentials"
            ) from None
        if not isinstance(data, dict):
            raise ValueError("Invalid Alpha Vantage response")
        for field in ("Error Message", "Note", "Information"):
            if field in data:
                raise ValueError(
                    f"Alpha Vantage returned {field}; check ticker, rate limit, and premium "
                    "access for TIME_SERIES_DAILY_ADJUSTED. No data was saved."
                )
        rows = data.get("Time Series (Daily)")
        metadata = data.get("Meta Data", {})
        if not isinstance(rows, dict) or not rows:
            raise ValueError("Alpha Vantage returned no adjusted daily history")
        if not isinstance(metadata, dict):
            raise ValueError("Invalid Alpha Vantage metadata")
        if metadata.get("2. Symbol", "").upper() != symbol:
            raise ValueError("Alpha Vantage symbol mismatch")
        if metadata.get("5. Time Zone") != "US/Eastern":
            raise ValueError("Only US/Eastern equity sessions are supported")
        result = []
        try:
            for day, row in sorted(rows.items()):
                utc_day(day)
                close = Decimal(row["5. adjusted close"])
                if not close.is_finite() or close <= 0:
                    raise ValueError("Invalid adjusted close")
                result.append(EquityDay(day, close))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ValueError("Invalid Alpha Vantage daily data") from None
        # Today's bar can still be provisional. Only store completed US sessions.
        return [r for r in result if r.available_at <= time.time()]


def save_equities(store, symbol, rows):
    symbol = equity_symbol(symbol)
    if not rows:
        raise ValueError("No completed equity sessions to save")
    with store.transaction() as db:
        for row in rows:
            body = encode(asdict(row))
            old = db.execute(
                "SELECT body FROM equity_daily WHERE symbol=? AND session_date=?",
                (symbol, row.session_date),
            ).fetchone()
            if old and old[0] != body:
                store.event(db, "equity_revision", {"symbol": symbol, "date": row.session_date})
            db.execute(
                "INSERT OR REPLACE INTO equity_daily VALUES(?,?,?,?)",
                (symbol, row.session_date, body, time.time()),
            )


def load_equities(store, symbol):
    with store.connect() as db:
        rows = db.execute(
            "SELECT body FROM equity_daily WHERE symbol=? ORDER BY session_date",
            (equity_symbol(symbol),),
        ).fetchall()
    return [
        EquityDay(r["session_date"], Decimal(r["adjusted_close"]))
        for r in (json.loads(row[0]) for row in rows)
    ]


class EquityFeatures:
    def __init__(self, symbol, rows):
        self.symbol = equity_symbol(symbol)
        self.rows = sorted(rows, key=lambda r: r.session_date)
        self.times = [r.available_at for r in self.rows]
        if len(set(self.times)) != len(self.times):
            raise ValueError("Duplicate equity sessions")

    def at(self, timestamp, sessions=20):
        index = bisect_right(self.times, timestamp) - 1
        if index < sessions or timestamp - self.times[index] > 4 * DAY:
            return None
        # Do not interpret a data outage as a long stock-market holiday.
        window = self.times[index - sessions : index + 1]
        if any(b - a > 4 * DAY for a, b in zip(window, window[1:])):
            return None
        return float(
            self.rows[index].adjusted_close / self.rows[index - sessions].adjusted_close - 1
        )

    def manifest(self, start, end):
        rows = [r for r in self.rows if r.available_at < end]
        return {
            "symbol": self.symbol,
            "source": "Alpha Vantage TIME_SERIES_DAILY_ADJUSTED",
            "count": len(rows),
            "first_session": rows[0].session_date if rows else None,
            "last_session": rows[-1].session_date if rows else None,
            "sha256": hashlib.sha256(encode([asdict(r) for r in rows]).encode()).hexdigest(),
            "decision_start": start,
            "decision_end": end,
        }

    def filter(self, timestamp, decision):
        momentum = self.at(timestamp)
        if momentum is None:
            raise ValueError(f"Incomplete equity history for {self.symbol} at {timestamp}")
        return {
            **decision,
            "action": decision["action"] if momentum > 0 else "exit",
            "equity_symbol": self.symbol,
            "equity_20_session_return": momentum,
        }


def correlation_report(
    store, start, end, symbols=DEFAULT_SYMBOLS, products=("BTC-USD", "ETH-USD", "SOL-USD")
):
    """Use cached daily crypto opens as subsequent targets. No downloads or tuning."""
    if start % DAY or end % DAY or start >= end:
        raise ValueError("Research requires increasing UTC dates")
    tests, inputs = [], []
    for symbol in symbols:
        features = EquityFeatures(symbol, load_equities(store, symbol))
        inputs.append(features.manifest(start, end))
        for product in products:
            candles = store.candles(start, end, DAY, product_id=product)
            try:
                validate_candles(candles, start, end)
            except ValueError as exc:
                tests.append(
                    {
                        "symbol": symbol,
                        "product_id": product,
                        "status": "incomplete",
                        "error": str(exc),
                    }
                )
                continue
            inputs.append(
                {
                    "product_id": product,
                    "start": start,
                    "end": end,
                    "count": len(candles),
                    "sha256": hashlib.sha256(
                        encode([asdict(c) for c in candles]).encode()
                    ).hexdigest(),
                }
            )
            for sessions in (1, 5, 20):
                for horizon in (1, 7):
                    pairs = []
                    for index in range(len(candles) - horizon):
                        x = features.at(candles[index].start, sessions)
                        if x is not None:
                            y = float(candles[index + horizon].open / candles[index].open - 1)
                            pairs.append((x, y))
                    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
                    sufficient = len(pairs) >= 30
                    varying = sufficient and statistics.pstdev(xs) > 0 and statistics.pstdev(ys) > 0
                    corr = statistics.correlation(xs, ys) if varying else None
                    tests.append(
                        {
                            "symbol": symbol,
                            "product_id": product,
                            "equity_sessions": sessions,
                            "crypto_horizon_days": horizon,
                            "samples": len(pairs),
                            "eligible_days": len(candles) - horizon,
                            "missing_feature_days": len(candles) - horizon - len(pairs),
                            "status": "complete" if sufficient else "incomplete",
                            "pearson": corr if corr is None or math.isfinite(corr) else None,
                            "mean_forward_return_equity_positive": statistics.mean(
                                [y for x, y in pairs if x > 0]
                            )
                            if any(x > 0 for x in xs)
                            else None,
                            "mean_forward_return_equity_nonpositive": statistics.mean(
                                [y for x, y in pairs if x <= 0]
                            )
                            if any(x <= 0 for x in xs)
                            else None,
                        }
                    )
    return {
        "mode": "equity-correlation",
        "start": start,
        "end": end,
        "tests": tests,
        "data_inputs": inputs,
        "assumptions": [
            "US adjusted close returns, usable from the next UTC midnight; no forward filling missing returns with zero.",
            "Targets are subsequent crypto UTC open-to-open returns; targets must end inside the requested partition.",
            "Equity features may repeat on weekends; seven-day targets overlap. Samples are not independent; no significance claims.",
            "Exploratory correlations and conditional gross returns are not tradable net-of-cost performance or proof of causality.",
            "Multiple symbols, windows and horizons are multiple hypotheses; do not select using evaluation results.",
            "Adjusted history is the current provider snapshot, not point-in-time vintages. Dividend/split revisions remain a limitation.",
            "2024–2025 has already been inspected for existing strategies; a new equity strategy needs a fresh untouched evaluation period.",
        ],
    }
