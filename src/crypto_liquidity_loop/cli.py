import argparse
import asyncio
import ipaddress
import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path

from .backtest import History, research_suite, run_backtest, utc_day
from .coinbase import CoinbasePublic
from .config import Config
from .domain import DAY
from .storage import Store, encode


def save_report(store, report, output):
    report_id = str(uuid.uuid4())
    report["id"] = report_id
    with store.transaction() as db:
        db.execute("INSERT INTO reports VALUES(?,?,?)", (report_id, time.time(), encode(report)))
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{report['label']}-{report_id}.json"
    target.write_text(json.dumps(report, indent=2))
    if report.get("status") == "incomplete":
        print(
            f"{report['asset']} {report['label']}: incomplete; {report['error']}; report {target}",
            flush=True,
        )
        return target
    m = report["metrics"]
    print(
        f"{report['label']}: strategy ${float(m['ending_wealth']):,.2f}; "
        f"{report.get('asset', 'BTC')} ${float(m['benchmark_wealth']):,.2f}; {m['fills']} fills; report {target}",
        flush=True,
    )
    return target


def main():
    parser = argparse.ArgumentParser(
        description="BTC/ETH/SOL research and BTC paper trading; real orders disabled"
    )
    parser.add_argument("--config", default="config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("doctor")
    commands.add_parser("status")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("--start", required=True)
    ingest.add_argument("--end", required=True, help="Exclusive UTC date")
    test = commands.add_parser("backtest")
    test.add_argument("--start", required=True)
    test.add_argument("--end", required=True, help="Exclusive UTC date")
    test.add_argument("--output", default="var/reports")
    filters = test.add_mutually_exclusive_group()
    filters.add_argument(
        "--equity-filter", help="Cached US ticker; fixed positive 20-session momentum gate"
    )
    filters.add_argument(
        "--market-filter",
        choices=("^NDQ", "^NDX"),
        help="Archive index; fixed positive 20-session momentum gate",
    )
    test.add_argument("--market-archive", default="backtesting/d_world_txt.zip")
    equities = commands.add_parser("equity-ingest")
    equities.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "IWM"])
    correlations = commands.add_parser("equity-research")
    correlations.add_argument("--start", required=True)
    correlations.add_argument("--end", required=True, help="Exclusive UTC date; cached data only")
    correlations.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "IWM"])
    correlations.add_argument("--output", default="var/equity-reports")
    archive = commands.add_parser("archive-research")
    archive.add_argument("--world", default="backtesting/d_world_txt.zip")
    archive.add_argument("--macro", default="backtesting/d_macro_txt.zip")
    archive.add_argument("--start", required=True)
    archive.add_argument("--end", required=True, help="Exclusive UTC date; offline only")
    archive.add_argument("--output", default="var/archive-reports")
    micro_download = commands.add_parser("microstructure-download")
    micro_download.add_argument("--month", required=True, help="Completed YYYY-MM month")
    micro_download.add_argument("--output", default="var/microstructure-data")
    micro = commands.add_parser("microstructure-research")
    micro.add_argument("--input", required=True, help="Canonical 5-minute CSV")
    micro.add_argument("--start", required=True)
    micro.add_argument("--end", required=True, help="Exclusive UTC date")
    micro.add_argument("--output", default="var/microstructure-reports")
    suite = commands.add_parser("research")
    suite.add_argument("--output", default="var/reports")
    for command in (ingest, test, suite):
        command.add_argument(
            "--product", choices=("BTC-USD", "ETH-USD", "SOL-USD"), default="BTC-USD"
        )
    commands.add_parser("worker")
    dashboard = commands.add_parser("dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8000)
    backup = commands.add_parser("backup")
    backup.add_argument("target")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config.load(args.config)
    if args.command == "doctor":
        print("Python: 3.12 required; execution mode: paper only")
        for key in (
            "LOOP_DASHBOARD_TOKEN",
            "LOOP_RESEND_API_KEY",
            "LOOP_EMAIL_FROM",
            "LOOP_EMAIL_TO",
            "ALPHAVANTAGE_API_KEY",
        ):
            print(f"{key}: {'present' if os.environ.get(key) else 'absent'}")
        try:
            version = subprocess.run(
                ["cdp", "--version"], capture_output=True, text=True, check=True
            )
            print(version.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            print("CDP CLI unavailable (public paper market data does not require it)")
        return
    if args.command in ("microstructure-download", "microstructure-research"):
        from zipfile import BadZipFile

        import httpx

        from .microstructure import download_cvd, event_study, load_csv, write_report

        try:
            if args.command == "microstructure-download":
                print(f"Normalized spot/perp CVD data: {download_cvd(args.month, args.output)}")
            else:
                rows, digest = load_csv(args.input)
                report = event_study(
                    rows, digest, utc_day(args.start), utc_day(args.end), costs=config
                )
                report["input_path"] = str(Path(args.input).resolve())
                manifest = Path(args.input).with_suffix(".manifest.json")
                if manifest.exists():
                    metadata = json.loads(manifest.read_text())
                    if metadata.get("normalized_sha256") != digest:
                        raise ValueError("Normalized data differs from its source manifest")
                    report["source_manifest"] = metadata
                target = write_report(report, args.output)
                for coverage in report["coverage"]:
                    print(
                        f"{coverage['asset']}: {coverage['status']}; {coverage['intervals']} intervals; "
                        f"unavailable families: {', '.join(coverage['missing_families']) or 'none'}"
                    )
                print(f"Microstructure event study: {target}")
        except (ValueError, OSError, BadZipFile, httpx.HTTPError) as exc:
            parser.error(str(exc))
        return
    store = Store(config.database)
    if args.command == "archive-research":
        from zipfile import BadZipFile

        from .archive_research import run_archive_research, write_archive_report

        try:
            report = run_archive_research(
                store, args.world, args.macro, utc_day(args.start), utc_day(args.end)
            )
            target, summary = write_archive_report(report, args.output)
        except (ValueError, OSError, BadZipFile, KeyError) as exc:
            parser.error(str(exc))
        complete = sum(t["status"] == "complete" for t in report["tests"])
        print(f"{complete}/{len(report['tests'])} complete market tests; report {target}")
        print(f"Readable summary: {summary}")
        return
    if args.command in ("equity-ingest", "equity-research"):
        from .equities import AlphaVantage, correlation_report, equity_symbol, save_equities

        try:
            symbols = list(dict.fromkeys(equity_symbol(s) for s in args.symbols))
            if args.command == "equity-ingest":
                client = AlphaVantage()
                try:
                    for symbol in symbols:
                        rows = client.daily(symbol)
                        save_equities(store, symbol, rows)
                        print(f"Stored {len(rows)} completed adjusted daily sessions for {symbol}")
                finally:
                    client.close()
            else:
                report = correlation_report(store, utc_day(args.start), utc_day(args.end), symbols)
                folder = Path(args.output)
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / f"equity-correlation-{args.start}-{args.end}-{uuid.uuid4()}.json"
                target.write_text(json.dumps(report, indent=2))
                complete = sum(r["status"] == "complete" for r in report["tests"])
                print(
                    f"{complete}/{len(report['tests'])} complete correlation tests; report {target}"
                )
        except ValueError as exc:
            parser.error(str(exc))
        return
    store.initialize(config)
    if args.command == "init":
        print(f"Initialized paper portfolio: ${config.initial_cash}; {store.path}")
    elif args.command == "status":
        print(json.dumps(store.state(), indent=2))
    elif args.command == "backup":
        store.backup(args.target)
        print(f"Consistent SQLite backup created: {args.target}")
    elif args.command == "dashboard":
        import uvicorn

        from .dashboard import create_app

        address = ipaddress.ip_address(args.host)
        tailscale = (
            address in ipaddress.ip_network("100.64.0.0/10") if address.version == 4 else False
        )
        if not (address.is_loopback or tailscale):
            parser.error(
                "Bind to loopback or a Tailscale IPv4 address; use a private tunnel for remote access"
            )
        if tailscale and not os.environ.get("LOOP_DASHBOARD_TOKEN"):
            parser.error("A dashboard token is required when binding to the private network")
        uvicorn.run(create_app(store, config), host=args.host, port=args.port)
    elif args.command == "worker":
        from .worker import run_worker

        try:
            asyncio.run(run_worker(store, config))
        except KeyboardInterrupt:
            print("Paper worker stopped")
    else:
        client = CoinbasePublic(product_id=args.product)
        try:
            history = History(store, client)
            if args.command == "ingest":
                candles = history.get(utc_day(args.start), utc_day(args.end), DAY)
                print(f"Verified and stored {len(candles)} complete UTC daily candles")
            elif args.command == "backtest":
                features = None
                if args.equity_filter:
                    from .equities import EquityFeatures, load_equities

                    features = EquityFeatures(
                        args.equity_filter, load_equities(store, args.equity_filter)
                    )
                elif args.market_filter:
                    from .archive_research import load_market_features

                    features = load_market_features(args.market_archive, args.market_filter)
                report = run_backtest(
                    config,
                    client.product(),
                    history,
                    utc_day(args.start),
                    utc_day(args.end),
                    label=f"equity-{features.symbol}" if features else "custom",
                    equity_features=features,
                )
                save_report(store, report, args.output)
            elif args.command == "research":
                reports = research_suite(config, client.product(), history)
                targets = [save_report(store, r, args.output) for r in reports]
                asset = args.product.split("-")[0]
                filename = (
                    "RESEARCH_SUMMARY.md" if asset == "BTC" else f"RESEARCH_SUMMARY_{asset}.md"
                )
                index = Path(args.output) / filename
                lines = [
                    f"# {asset} research results",
                    "",
                    "Candle execution is approximate; live trading remains disabled.",
                    "No parameters were selected using held-out results.",
                    "",
                    f"| Period | Strategy | {asset} buy-and-hold | Max drawdown | Fills |",
                    "| --- | ---: | ---: | ---: | ---: |",
                ]
                lines += reports[0].get("coverage_notes", []) + [""]
                for report, target in zip(reports, targets, strict=True):
                    if report.get("status") == "incomplete":
                        lines.append(
                            f"| [{report['label']}]({target.name}) | Incomplete history | — | — | — |"
                        )
                        continue
                    m = report["metrics"]
                    lines.append(
                        f"| [{report['label']}]({target.name}) | ${float(m['ending_wealth']):,.2f} | "
                        f"${float(m['benchmark_wealth']):,.2f} | {float(m['max_drawdown']):.2%} | {m['fills']} |"
                    )
                lines += [
                    "",
                    "Operating costs, liquidated wealth, parameters, data hashes, fees, and assumptions are included in each JSON report.",
                    "Walk-forward reports apply the fixed strategy to successive years; they do not perform parameter optimization.",
                ]
                index.write_text("\n".join(lines) + "\n")
                print(f"Research summary: {index}")
        finally:
            client.close()


if __name__ == "__main__":
    main()
