"""Offline research from user-supplied daily TXT archives; preserve data provenance."""

import csv
import hashlib
import io
import json
import statistics
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile

from .backtest import utc_day
from .domain import DAY, validate_candles
from .storage import encode

# Fixed exploratory hypotheses, not chosen by ranking results.
MARKETS = {
    "^NDQ": ("indices/^ndq.txt", "return"),
    "^NDX": ("indices/^ndx.txt", "return"),
    "EURUSD": ("currencies/major/eurusd.txt", "return"),
    "2YUSY.B": ("bonds/2yusy.b.txt", "difference"),
    "10YUSY.B": ("bonds/10yusy.b.txt", "difference"),
}
MACROS = ("CPIYUS.M", "UNRTUS.M", "INRTUS.M", "GDPQUS.M")
PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD")


@dataclass(frozen=True)
class Observation:
    date: str
    close: Decimal
    volume: Decimal

    @property
    def timestamp(self):
        return utc_day(self.date)


def read_series(archive, member, ticker):
    """Read selected members only; never extract user-controlled ZIP paths."""
    info = archive.getinfo(member)
    if info.file_size > 30_000_000:
        raise ValueError(f"Oversized member: {member}")
    raw = archive.read(member)
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    required = {"<TICKER>", "<PER>", "<DATE>", "<TIME>", "<CLOSE>", "<VOL>"}
    if not required.issubset(rows.fieldnames or ()):
        raise ValueError(f"Invalid daily TXT header: {member}")
    result = []
    previous = None
    try:
        for row in rows:
            if row["<TICKER>"] != ticker or row["<PER>"] != "D" or row["<TIME>"] != "000000":
                raise ValueError("Ticker, frequency or time mismatch")
            date = datetime.strptime(row["<DATE>"], "%Y%m%d").strftime("%Y-%m-%d")
            if previous is not None and date <= previous:
                raise ValueError("Duplicate or unordered observation dates")
            previous = date
            close, volume = Decimal(row["<CLOSE>"]), Decimal(row["<VOL>"])
            if not close.is_finite() or not volume.is_finite() or volume < 0:
                raise ValueError("Invalid numeric observation")
            result.append(Observation(date, close, volume))
    except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
        raise ValueError(f"Invalid series {member}: {exc}") from None
    if not result:
        raise ValueError(f"Empty series: {member}")
    return result, hashlib.sha256(raw).hexdigest()


def series_manifest(rows, member, digest):
    return {
        "member": member,
        "sha256": digest,
        "observations": len(rows),
        "first_date": rows[0].date,
        "last_date": rows[-1].date,
        "zero_volume_rows": sum(r.volume == 0 for r in rows),
        "latest_value": str(rows[-1].close),
    }


class MarketFeatures:
    def __init__(self, symbol, rows, transform, manifest):
        self.symbol, self.rows, self.transform = symbol, rows, transform
        self.times = [r.timestamp + DAY for r in rows]
        self.input = manifest
        if transform not in ("return", "difference"):
            raise ValueError("Unsupported market transform")
        if transform == "return" and any(r.close <= 0 for r in rows):
            raise ValueError(f"Nonpositive market prices: {symbol}")

    def at(self, timestamp, sessions=20):
        index = bisect_right(self.times, timestamp) - 1
        if index < sessions or timestamp - self.times[index] > 4 * DAY:
            return None
        window = self.times[index - sessions : index + 1]
        if any(b - a > 4 * DAY for a, b in zip(window, window[1:])):
            return None
        latest, prior = self.rows[index].close, self.rows[index - sessions].close
        return float(latest / prior - 1 if self.transform == "return" else latest - prior)

    def manifest(self, start, end):
        used = [r for r in self.rows if r.timestamp + DAY < end]
        return {
            **self.input,
            "symbol": self.symbol,
            "transform": self.transform,
            "decision_start": start,
            "decision_end": end,
            "usable_input_sha256": hashlib.sha256(
                encode([{"date": r.date, "close": str(r.close)} for r in used]).encode()
            ).hexdigest(),
        }

    def filter(self, timestamp, decision):
        if self.transform != "return":
            raise ValueError("Momentum gate only supports price returns")
        value = self.at(timestamp)
        if value is None:
            raise ValueError(f"Incomplete market history for {self.symbol} at {timestamp}")
        return {
            **decision,
            "action": decision["action"] if value > 0 else "exit",
            "equity_symbol": self.symbol,
            "equity_20_session_return": value,
        }


def paired_test(features, candles, sessions, horizon):
    pairs = []
    for index in range(len(candles) - horizon):
        x = features.at(candles[index].start, sessions)
        if x is not None:
            y = float(candles[index + horizon].open / candles[index].open - 1)
            pairs.append((x, y))
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    corr = None
    if len(pairs) >= 30 and statistics.pstdev(xs) > 0 and statistics.pstdev(ys) > 0:
        corr = statistics.correlation(xs, ys)
    positive = [y for x, y in pairs if x > 0]
    other = [y for x, y in pairs if x <= 0]
    return {
        "sessions": sessions,
        "horizon_days": horizon,
        "samples": len(pairs),
        "missing_feature_days": len(candles) - horizon - len(pairs),
        "status": "complete" if len(pairs) >= 30 else "incomplete",
        "pearson": corr,
        "positive_feature_samples": len(positive),
        "nonpositive_feature_samples": len(other),
        "mean_forward_return_positive": statistics.mean(positive) if positive else None,
        "mean_forward_return_nonpositive": statistics.mean(other) if other else None,
    }


def archive_inventory(path):
    with ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.endswith(".txt")]
        categories = Counter("/".join(n.split("/")[2:-1]) for n in names)
    with Path(path).open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "path": str(path),
        "sha256": digest,
        "series_count": len(names),
        "categories": dict(sorted(categories.items())),
    }


def load_market_features(path, symbol):
    if symbol not in MARKETS:
        raise ValueError("Unsupported archive market")
    suffix, transform = MARKETS[symbol]
    with ZipFile(path) as archive:
        member = "data/daily/world/" + suffix
        rows, digest = read_series(archive, member, symbol)
    manifest = series_manifest(rows, member, digest)
    manifest.update(
        source="User-supplied daily TXT archive; adjustment method unverified",
        archive_sha256=archive_inventory(path)["sha256"],
    )
    return MarketFeatures(symbol, rows, transform, manifest)


def run_archive_research(store, world_path, macro_path, start, end):
    if start % DAY or end % DAY or end - start < 8 * DAY:
        raise ValueError("Archive research requires at least eight increasing UTC days")
    inventory = [archive_inventory(world_path), archive_inventory(macro_path)]
    markets, macro, crypto_archive = {}, [], []
    with ZipFile(world_path) as archive:
        for symbol, (suffix, transform) in MARKETS.items():
            member = "data/daily/world/" + suffix
            rows, digest = read_series(archive, member, symbol)
            manifest = series_manifest(rows, member, digest)
            manifest["archive_sha256"] = inventory[0]["sha256"]
            markets[symbol] = MarketFeatures(symbol, rows, transform, manifest)
        for symbol in ("BTC", "ETH", "SOL"):
            member = f"data/daily/world/cryptocurrencies/{symbol.lower()}.v.txt"
            rows, digest = read_series(archive, member, symbol + ".V")
            crypto_archive.append(
                {
                    "symbol": symbol,
                    **series_manifest(rows, member, digest),
                    "usage": "coverage only; execution uses Coinbase data",
                }
            )
    with ZipFile(macro_path) as archive:
        for symbol in MACROS:
            member = f"data/daily/macro/us/{symbol.lower()}.txt"
            rows, digest = read_series(archive, member, symbol)
            macro.append(
                {
                    "symbol": symbol,
                    **series_manifest(rows, member, digest),
                    "status": "excluded_from_predictive_tests",
                    "reason": "No release timestamps or point-in-time vintages supplied",
                }
            )
    tests, inputs = [], []
    for product in PRODUCTS:
        candles = store.candles(start, end, DAY, product_id=product)
        try:
            validate_candles(candles, start, end)
        except ValueError as exc:
            tests.append({"product_id": product, "status": "incomplete", "error": str(exc)})
            continue
        inputs.append(
            {
                "product_id": product,
                "count": len(candles),
                "sha256": hashlib.sha256(
                    encode([{"start": c.start, "open": str(c.open)} for c in candles]).encode()
                ).hexdigest(),
            }
        )
        for symbol, features in markets.items():
            for sessions in (1, 5, 20):
                for horizon in (1, 7):
                    tests.append(
                        {
                            "product_id": product,
                            "market": symbol,
                            "transform": features.transform,
                            **paired_test(features, candles, sessions, horizon),
                        }
                    )
    return {
        "mode": "archive-market-research",
        "start": start,
        "end": end,
        "archives": inventory,
        "market_inputs": [f.manifest(start, end) for f in markets.values()],
        "crypto_inputs": inputs,
        "archive_crypto_coverage": crypto_archive,
        "macro_inventory": macro,
        "tests": tests,
        "assumptions": [
            "Market observations become usable the next UTC midnight; actual close/publication timezone is unverified in the archives.",
            "Coinbase UTC crypto opens supply future 1/7-day return targets; targets end strictly inside this partition.",
            "Index and FX features use close returns; yields use absolute changes in source units, not bond returns.",
            "Price adjustment methodology and historical revisions are unverified; these are exploratory results.",
            "Four-day maximum staleness/gaps; missing features are excluded and counted.",
            "Weekend features repeat; seven-day targets overlap; sample counts are not independent observations.",
            "Many tested hypotheses increase false-discovery risk. No automatic ranking selects a strategy.",
            "Gross conditional returns are not executable strategies or proof of net performance.",
            "Macro reference-period dates are not release dates. No macro series is used for predictive tests or trades.",
            "Existing 2024–2025 evaluation has been inspected; any new selected strategy needs fresh untouched data.",
            "Archive crypto observations do not replace Coinbase candles or minute execution data.",
        ],
    }


def write_archive_report(report, output):
    start = datetime.fromtimestamp(report["start"], timezone.utc).date()
    end = datetime.fromtimestamp(report["end"], timezone.utc).date()
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"archive-research-{start}-{end}.json"
    target.write_text(json.dumps(report, indent=2))
    lines = [
        f"# Archive cross-market research: {start} to {end}",
        "",
        "Exploratory lagged correlations, not evidence of profitable trading.",
        "",
        "| Asset | Feature | Window | Target | Pairs | Pearson | Missing days |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["tests"]:
        if "market" not in row:
            lines.append(f"| {row['product_id']} | Incomplete crypto history | — | — | — | — | — |")
            continue
        corr = f"{row['pearson']:.4f}" if row["pearson"] is not None else "—"
        lines.append(
            f"| {row['product_id']} | {row['market']} ({row['transform']}) | "
            f"{row['sessions']} | {row['horizon_days']}d | {row['samples']} | {corr} | "
            f"{row['missing_feature_days']} |"
        )
    lines += [
        "",
        "## Macro availability",
        "",
        "Macro series are inventoried but excluded from predictive tests: release timestamps and historical vintages are absent.",
        "",
        "| Series | First observation | Last observation | Rows |",
        "| --- | --- | --- | ---: |",
    ]
    for r in report["macro_inventory"]:
        lines.append(
            f"| {r['symbol']} | {r['first_date']} | {r['last_date']} | {r['observations']} |"
        )
    lines += ["", "## Assumptions", ""] + [f"- {a}" for a in report["assumptions"]]
    summary = target.with_suffix(".md")
    summary.write_text("\n".join(lines) + "\n")
    return target, summary
