"""Intraday microstructure hypotheses; missing feeds never imply neutral signals."""

import csv
import hashlib
import io
import json
import math
import statistics
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import certifi
import httpx

ASSETS = ("BTC", "ETH", "SOL")
FLOW = ("spot_buy_quote", "spot_sell_quote", "perp_buy_quote", "perp_sell_quote")
NUMERIC = (
    "open",
    "high",
    "low",
    "close",
    *FLOW,
    "oi_base",
    "funding_rate",
    "funding_interval_hours",
    "bid_depth_quote",
    "ask_depth_quote",
    "spread_bps",
    "book_band_bps",
)
FLAGS = ("spot_complete", "perp_complete", "book_complete")
SOURCE_TIMES = ("oi_timestamp", "funding_timestamp", "book_timestamp")
COLUMNS = (
    "asset",
    "timestamp",
    "available_at",
    "spot_venue",
    "perp_venue",
    "book_venue",
    *NUMERIC,
    *SOURCE_TIMES,
    *FLAGS,
)
SIGNALS = (
    "spot_absorption",
    "perp_only_rally",
    "crowded_longs",
    "crowded_shorts",
    "long_flush_trigger",
    "short_squeeze_trigger",
    "ask_liquidity_squeeze",
)


@dataclass(frozen=True)
class Rules:
    step: int = 300
    window: int = 12
    spot_delta_min: float = 0.10
    spot_flat_max: float = 0.02
    perp_delta_min: float = 0.10
    sideways_max: float = 0.002
    consolidation_range_max: float = 0.01
    oi_growth_min: float = 0.05
    funding_per_8h_extreme: float = 0.0003
    taker_ratio_long: float = 1.2
    taker_ratio_short: float = 0.8
    trigger_move: float = 0.002
    depth_drop: float = 0.50
    depth_imbalance_min: float = 2
    book_band_bps: float = 25
    spread_max_bps: float = 10
    book_persistence: int = 3


def load_csv(path):
    raw = Path(path).read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if not set(COLUMNS).issubset(reader.fieldnames or ()):
        raise ValueError("Microstructure CSV must use the documented canonical columns")
    rows, seen = [], set()
    for number, data in enumerate(reader, 2):
        try:
            row = {k: data[k] for k in ("asset", "spot_venue", "perp_venue", "book_venue")}
            if row["asset"] not in ASSETS:
                raise ValueError("Unsupported asset")
            for key in ("timestamp", "available_at"):
                row[key] = int(data[key])
            if row["timestamp"] <= 0 or row["available_at"] < row["timestamp"]:
                raise ValueError("Availability precedes interval completion")
            if row["timestamp"] % 300:
                raise ValueError("Expected UTC-aligned 5-minute interval ends")
            for key in SOURCE_TIMES:
                row[key] = int(data[key]) if data[key] else None
                if row[key] is not None and not 0 < row[key] <= row["timestamp"]:
                    raise ValueError("Invalid/future source timestamp")
            for key in NUMERIC:
                row[key] = float(data[key]) if data[key] != "" else None
                if row[key] is not None and not math.isfinite(row[key]):
                    raise ValueError("Nonfinite input")
            for key in FLAGS:
                if data[key] not in ("true", "false"):
                    raise ValueError("Completeness flags must be true/false")
                row[key] = data[key] == "true"
            if any(row[k] is None or row[k] <= 0 for k in ("open", "high", "low", "close")):
                raise ValueError("Missing or nonpositive prices")
            if (
                not row["low"]
                <= min(row["open"], row["close"])
                <= max(row["open"], row["close"])
                <= row["high"]
            ):
                raise ValueError("Invalid OHLC")
            for key in (
                *FLOW,
                "oi_base",
                "bid_depth_quote",
                "ask_depth_quote",
                "spread_bps",
                "book_band_bps",
            ):
                if row[key] is not None and row[key] < 0:
                    raise ValueError("Negative quantity/depth")
            if row["funding_interval_hours"] is not None and row["funding_interval_hours"] <= 0:
                raise ValueError("Invalid funding interval")
            for market in ("spot", "perp"):
                if row[f"{market}_complete"] and (
                    not row[f"{market}_venue"]
                    or any(row[f"{market}_{s}_quote"] is None for s in ("buy", "sell"))
                ):
                    raise ValueError("Complete flow requires venue and both volume sides")
            if row["book_complete"] and (
                not row["book_venue"]
                or any(
                    row[k] is None
                    for k in (
                        "bid_depth_quote",
                        "ask_depth_quote",
                        "spread_bps",
                        "book_band_bps",
                        "book_timestamp",
                    )
                )
            ):
                raise ValueError("Complete book requires venue, depth and band metadata")
            key = (row["asset"], row["timestamp"])
            if key in seen:
                raise ValueError("Duplicate asset/interval")
            seen.add(key)
            rows.append(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"CSV row {number}: {exc}") from None
    return sorted(rows, key=lambda r: (r["asset"], r["timestamp"])), hashlib.sha256(raw).hexdigest()


def normalized_delta(rows, market):
    if not all(
        r[f"{market}_complete"] and r[f"{market}_venue"] == rows[0][f"{market}_venue"] for r in rows
    ):
        return None
    buy = sum(r[f"{market}_buy_quote"] for r in rows)
    sell = sum(r[f"{market}_sell_quote"] for r in rows)
    return (buy - sell) / (buy + sell) if buy + sell else None


def detect(rows, index, rules=Rules()):
    current = rows[index]
    unavailable = {key: None for key in SIGNALS}
    if index < rules.window:
        return {"status": "warmup", "signals": unavailable, "features": {}}
    history = rows[index - rules.window : index + 1]
    now = current["available_at"]
    if any(b["timestamp"] - a["timestamp"] != rules.step for a, b in zip(history, history[1:])):
        return {"status": "gap", "signals": unavailable, "features": {}}
    if any(r["available_at"] > now for r in history) or now - current["timestamp"] > rules.step:
        return {"status": "unavailable_or_stale", "signals": unavailable, "features": {}}
    # Compare price against the close preceding the flow window.
    window = history[1:]
    change = current["close"] / history[0]["close"] - 1
    spot, perp = normalized_delta(window, "spot"), normalized_delta(window, "perp")
    signals = dict(unavailable)
    features = {
        "price_return": change,
        "spot_normalized_delta": spot,
        "perp_normalized_delta": perp,
    }
    for market, delta in (("spot", spot), ("perp", perp)):
        features[f"{market}_cvd_change_quote"] = (
            sum(r[f"{market}_buy_quote"] - r[f"{market}_sell_quote"] for r in window)
            if delta is not None
            else None
        )
    if spot is not None:
        # Positive net aggressive spot flow in each third of the observation window.
        thirds = [
            window[i : i + rules.window // 3] for i in range(0, rules.window, rules.window // 3)
        ]
        persistent = all((normalized_delta(part, "spot") or 0) > 0 for part in thirds)
        signals["spot_absorption"] = (
            -rules.consolidation_range_max <= change <= rules.sideways_max
            and spot >= rules.spot_delta_min
            and persistent
        )
    if spot is not None and perp is not None:
        signals["perp_only_rally"] = (
            change > rules.sideways_max
            and spot <= rules.spot_flat_max
            and perp >= rules.perp_delta_min
        )
    # Build-up and crowding precede the trigger bar. Base OI avoids price-induced USD OI changes.
    prior = rows[index - rules.window - 1 : index] if index > rules.window else []
    if (
        len(prior) == rules.window + 1
        and all(r["available_at"] <= now for r in prior)
        and all(b["timestamp"] - a["timestamp"] == rules.step for a, b in zip(prior, prior[1:]))
        and all(
            r["oi_base"] is not None
            and r["oi_timestamp"] is not None
            and 0 <= r["timestamp"] - r["oi_timestamp"] <= rules.step
            for r in prior
        )
        and prior[0]["oi_base"] > 0
        and all(r["perp_venue"] == current["perp_venue"] and r["perp_venue"] for r in prior)
        and prior[-1]["funding_rate"] is not None
        and prior[-1]["funding_interval_hours"] is not None
        and prior[-1]["funding_timestamp"] is not None
        and prior[-1]["timestamp"] - prior[-1]["funding_timestamp"]
        <= prior[-1]["funding_interval_hours"] * 3600
    ):
        oi_growth = prior[-1]["oi_base"] / prior[0]["oi_base"] - 1
        funding = prior[-1]["funding_rate"] * 8 / prior[-1]["funding_interval_hours"]
        price_range = max(r["high"] for r in prior[1:]) / min(r["low"] for r in prior[1:]) - 1
        delta = normalized_delta(prior[1:], "perp")
        ratio = (1 + delta) / (1 - delta) if delta is not None and delta < 1 else None
        # No denominator or no perp coverage means unknown, not a squeeze prediction.
        if ratio is not None:
            setup = (
                oi_growth >= rules.oi_growth_min and price_range <= rules.consolidation_range_max
            )
            signals["crowded_longs"] = (
                setup
                and funding >= rules.funding_per_8h_extreme
                and ratio >= rules.taker_ratio_long
            )
            signals["crowded_shorts"] = (
                setup
                and funding <= -rules.funding_per_8h_extreme
                and ratio <= rules.taker_ratio_short
            )
            tick = current["close"] / prior[-1]["close"] - 1
            signals["long_flush_trigger"] = signals["crowded_longs"] and tick <= -rules.trigger_move
            signals["short_squeeze_trigger"] = (
                signals["crowded_shorts"] and tick >= rules.trigger_move
            )
            features.update(
                oi_base_growth=oi_growth,
                funding_per_8h=funding,
                taker_buy_sell_ratio=ratio,
                consolidation_range=price_range,
            )
    books = history
    if all(
        r["book_complete"]
        and r["book_venue"] == current["book_venue"]
        and r["book_band_bps"] == rules.book_band_bps
        and r["book_timestamp"] is not None
        and 0 <= r["timestamp"] - r["book_timestamp"] <= rules.step
        for r in books
    ):
        baseline = statistics.median(r["ask_depth_quote"] for r in books[: -rules.book_persistence])
        latest = books[-rules.book_persistence :]
        if baseline > 0 and spot is not None:
            signals["ask_liquidity_squeeze"] = (
                all(
                    r["ask_depth_quote"] > 0
                    and r["ask_depth_quote"] <= baseline * (1 - rules.depth_drop)
                    and r["bid_depth_quote"] / r["ask_depth_quote"] >= rules.depth_imbalance_min
                    and r["spread_bps"] <= rules.spread_max_bps
                    for r in latest
                )
                and spot > 0
            )
            features["ask_depth_vs_baseline"] = current["ask_depth_quote"] / baseline
    return {"status": "evaluated", "signals": signals, "features": features}


def event_study(rows, digest, start, end, rules=Rules(), costs=None):
    if start >= end:
        raise ValueError("Increasing research times required")
    cost_factor = None
    if costs is not None:
        fee = float(costs.taker_fee)
        spread = float(costs.spread_bps) / 20000
        slip = float(costs.slippage_bps) / 10000
        cost_factor = (1 - fee) / (1 + fee) * (1 - spread) / (1 + spread) * (1 - slip) / (1 + slip)
    records, tests, coverage = [], [], []
    for asset in ASSETS:
        data = [r for r in rows if r["asset"] == asset and r["timestamp"] < end]
        evaluated = [
            (i, detect(data, i, rules)) for i, r in enumerate(data) if start <= r["timestamp"] < end
        ]
        for i, result in evaluated:
            records.append(
                {
                    "asset": asset,
                    "timestamp": data[i]["timestamp"],
                    "available_at": data[i]["available_at"],
                    **result,
                }
            )
        missing_families = [
            family
            for family, keys in {
                "cvd": ("spot_absorption", "perp_only_rally"),
                "oi_funding": ("short_squeeze_trigger", "long_flush_trigger"),
                "book": ("ask_liquidity_squeeze",),
            }.items()
            if not any(any(res["signals"][k] is not None for k in keys) for _, res in evaluated)
        ]
        coverage.append(
            {
                "asset": asset,
                "intervals": len(evaluated),
                "status": (
                    "incomplete" if not evaluated else "partial" if missing_families else "complete"
                ),
                "missing_families": missing_families,
                "available_signal_intervals": {
                    name: sum(res["signals"][name] is not None for _, res in evaluated)
                    for name in SIGNALS
                },
            }
        )
        for name in SIGNALS:
            for horizon in (3, 12):
                values, baseline = [], []
                last_exit, skipped = -1, 0
                for i, result in evaluated:
                    # Execute at the next FULL interval open strictly after all feature availability.
                    entry = i + 1
                    while (
                        entry < len(data)
                        and data[entry]["timestamp"] - rules.step <= data[i]["available_at"]
                    ):
                        entry += 1
                    exit_ = entry + horizon
                    if exit_ >= len(data) or data[exit_]["timestamp"] >= end:
                        continue
                    path = data[i : exit_ + 1]
                    if any(
                        b["timestamp"] - a["timestamp"] != rules.step
                        for a, b in zip(path, path[1:])
                    ):
                        continue
                    forward = data[exit_]["open"] / data[entry]["open"] - 1
                    if result["signals"][name] is not None:
                        baseline.append(forward)
                    if result["signals"][name] is True:
                        if entry < last_exit:
                            skipped += 1
                            continue
                        values.append(forward)
                        last_exit = exit_
                tests.append(
                    {
                        "asset": asset,
                        "signal": name,
                        "horizon_minutes": horizon * rules.step // 60,
                        "events": len(values),
                        "overlapping_events_skipped": skipped,
                        "eligible_intervals": len(baseline),
                        "status": "complete" if len(values) >= 30 else "insufficient_events",
                        "mean_forward_spot_return": statistics.mean(values) if values else None,
                        "mean_unconditional_return": statistics.mean(baseline)
                        if baseline
                        else None,
                        "illustrative_mean_long_return_after_costs": statistics.mean(
                            (1 + v) * cost_factor - 1 for v in values
                        )
                        if values and cost_factor is not None
                        else None,
                    }
                )
    return {
        "mode": "microstructure-event-study",
        "start": start,
        "end": end,
        "input_sha256": digest,
        "rules": asdict(rules),
        "illustrative_costs": {
            "taker_fee": str(costs.taker_fee),
            "spread_bps": str(costs.spread_bps),
            "slippage_bps": str(costs.slippage_bps),
            "definition": "Both legs taker; full buy/sell spread and slippage; no operating cost or queue/impact simulation",
        }
        if costs is not None
        else None,
        "coverage": coverage,
        "tests": tests,
        "observations": records,
        "assumptions": [
            "Exploratory fixed 5-minute hypotheses; no threshold selection or claims of guaranteed reversals/liquidations.",
            "Signals use completed intervals and availability timestamps. Entry uses a later full interval open, never the signal close.",
            "Reported spot returns are event outcomes, not portfolio results. Optional cost-adjusted long returns are arithmetic sensitivities, not simulated fills; no funding PnL or short profits are modeled.",
            "Negative-direction alerts are risk warnings, not permission to short. Live and paper trading rules are unaffected.",
            "Event windows do not overlap within a signal/horizon, but baseline targets overlap and cross-signal outcomes are dependent.",
            "Missing feeds remain unknown; daily OHLCV cannot reconstruct aggressive flow, OI, funding or book depth.",
            "Spot/perp deltas are normalized separately by quote volume, not compared as raw cumulative levels across venues.",
            "Funding is linearly scaled to an 8-hour convention; base-asset OI is required, not USD notional OI.",
            "Depth uses a consistent 25-bps band and complete snapshots; thinning requires three intervals and positive spot flow.",
            "Historical provider publication delay is an assumption unless directly recorded; retrospective archives are not feed latency evidence.",
        ],
    }


def parse_klines(raw, asset, market, step=300):
    """Binance spot/USD-M kline schema, including spot microsecond timestamps."""
    if asset not in ASSETS or market not in ("spot", "perp"):
        raise ValueError("Unsupported asset/market")
    parsed = {}
    for fields in csv.reader(io.StringIO(raw.decode("utf-8-sig"))):
        if fields and fields[0] == "open_time":
            continue
        if len(fields) != 12:
            raise ValueError("Expected Binance 12-column klines")
        start_raw, close_raw = int(fields[0]), int(fields[6])
        unit = 1_000_000 if start_raw >= 100_000_000_000_000 else 1000
        start = start_raw // unit
        if start % step or not step - 1 <= close_raw / unit - start < step:
            raise ValueError("Unaligned or unsupported kline interval")
        end = start + step
        quote, taker = float(fields[7]), float(fields[10])
        if not math.isfinite(quote) or not math.isfinite(taker) or not 0 <= taker <= quote:
            raise ValueError("Invalid taker/quote volume")
        if end in parsed:
            raise ValueError("Duplicate kline interval")
        row = {k: "" for k in COLUMNS}
        row.update(
            asset=asset,
            timestamp=end,
            available_at=end + 1,
            spot_complete="false",
            perp_complete="false",
            book_complete="false",
        )
        row.update(
            {
                f"{market}_venue": "binance-usdt",
                f"{market}_buy_quote": taker,
                f"{market}_sell_quote": quote - taker,
                f"{market}_complete": "true",
            }
        )
        if market == "spot":
            row.update(dict(zip(("open", "high", "low", "close"), fields[1:5])))
        parsed[end] = row
    return parsed


def download_cvd(month, output):
    """Explicit six-archive download with checksums; no derivatives trading API calls."""
    date = datetime.strptime(month, "%Y-%m")
    if month != date.strftime("%Y-%m") or date.replace(tzinfo=timezone.utc) >= datetime.now(
        timezone.utc
    ).replace(day=1, hour=0, minute=0, second=0, microsecond=0):
        raise ValueError("Use a completed calendar month in YYYY-MM format")
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    manifests, combined = [], []
    with httpx.Client(timeout=60, verify=certifi.where()) as client:
        for asset in ASSETS:
            markets = {}
            for market, prefix in (("spot", "spot"), ("perp", "futures/um")):
                filename = f"{asset}USDT-5m-{month}.zip"
                url = f"https://data.binance.vision/data/{prefix}/monthly/klines/{asset}USDT/5m/{filename}"
                target = folder / f"{market}-{filename}"
                checksum_path = target.with_suffix(".zip.CHECKSUM")
                if not target.exists() or not checksum_path.exists():
                    response = client.get(url)
                    response.raise_for_status()
                    checksum = client.get(url + ".CHECKSUM")
                    checksum.raise_for_status()
                    target.write_bytes(response.content)
                    checksum_path.write_text(checksum.text)
                raw = target.read_bytes()
                expected = checksum_path.read_text().split()[0]
                digest = hashlib.sha256(raw).hexdigest()
                if digest != expected:
                    raise ValueError(f"Archive checksum mismatch: {target}")
                with ZipFile(io.BytesIO(raw)) as archive:
                    members = [n for n in archive.namelist() if n.endswith(".csv")]
                    if len(members) != 1 or archive.getinfo(members[0]).file_size > 30_000_000:
                        raise ValueError("Unexpected kline archive members/size")
                    markets[market] = parse_klines(archive.read(members[0]), asset, market)
                manifests.append({"asset": asset, "market": market, "url": url, "sha256": digest})
            if set(markets["spot"]) != set(markets["perp"]):
                raise ValueError(f"Spot/perp coverage mismatch for {asset}")
            for end in sorted(markets["spot"]):
                row = dict(markets["spot"][end])
                row.update({k: v for k, v in markets["perp"][end].items() if k.startswith("perp_")})
                combined.append(row)
    target = folder / f"cvd-{month}.csv"
    with target.open("w", newline="") as dest:
        writer = csv.DictWriter(dest, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(combined)
    # Revalidate the normalized output before making it available to research.
    _, digest = load_csv(target)
    target.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "archives": manifests,
                "normalized_sha256": digest,
                "availability_assumption": "Interval close + 1 second; actual historical publication latency unverified",
                "missing": ["base OI", "funding", "order-book depth"],
                "quote_currency": "USDT; not interchangeable with Coinbase USD without basis analysis",
            },
            indent=2,
        )
    )
    return target


def write_report(report, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"microstructure-{uuid.uuid4()}.json"
    target.write_text(json.dumps(report, indent=2))
    lines = [
        "# Microstructure event study",
        "",
        "Fixed hypotheses; event returns do not establish portfolio performance.",
        "",
        "| Asset | Signal | Horizon | Events | Gross mean return | Illustrative long return after costs | Status |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report["tests"]:
        gross = (
            f"{row['mean_forward_spot_return']:.4%}"
            if row["mean_forward_spot_return"] is not None
            else "—"
        )
        net = (
            f"{row['illustrative_mean_long_return_after_costs']:.4%}"
            if row["illustrative_mean_long_return_after_costs"] is not None
            else "—"
        )
        lines.append(
            f"| {row['asset']} | {row['signal']} | {row['horizon_minutes']}m | {row['events']} | {gross} | {net} | {row['status']} |"
        )
    lines += ["", "## Coverage", ""]
    for row in report["coverage"]:
        lines.append(
            f"- {row['asset']}: {row['status']}; {row['intervals']} intervals; unavailable families: {', '.join(row['missing_families']) or 'none'}."
        )
    lines += ["", "## Assumptions", ""] + [f"- {s}" for s in report["assumptions"]]
    target.with_suffix(".md").write_text("\n".join(lines) + "\n")
    return target
