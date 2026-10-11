# Crypto Liquidity Loop

BTC-USD, ETH-USD and SOL-USD historical research and continuous BTC paper trading, with a private FastAPI dashboard.
The strategy uses a 55-day breakout, 20-day exit, and 200-day SMA. Funds and orders
are simulated. **This release cannot submit real orders.**

## Local setup

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --frozen
cp config.example.toml config.toml
uv run liquidity-loop init
uv run liquidity-loop doctor
uv run pytest
```

Run the dashboard and worker in separate terminals:

```bash
uv run liquidity-loop dashboard
uv run liquidity-loop worker
```

Open <http://127.0.0.1:8000>. Without an owner token, the local dashboard is read-only.
For controls, set `LOOP_DASHBOARD_TOKEN` to a randomly generated token of at least
32 characters **before starting the dashboard**, then sign in at `/login`.
The worker alone processes control requests. Never put tokens in URLs.

The worker uses public Coinbase REST data and a Level 2 WebSocket book, with REST
quote recovery after disconnects. No trade-permitted credentials are required.
Each database represents one immutable configuration and one $500 simulated run;
use another database path to change trading or fee settings.

## Historical research

```bash
uv run liquidity-loop ingest --start 2018-01-01 --end 2026-01-01
uv run liquidity-loop backtest --start 2024-01-01 --end 2026-01-01
uv run liquidity-loop research
uv run liquidity-loop research --product ETH-USD
uv run liquidity-loop research --product SOL-USD
```

End dates are exclusive UTC boundaries. History is downloaded and cached in SQLite;
missing or duplicate candles fail validation. Minute data is downloaded only for
execution windows. Reports appear in `var/reports/` and the dashboard.

The research suite freezes training (2019–2021), validation (2022–2023), and held-out
(2024–2025) periods before results are inspected. It includes validation-only nearby
and slower parameters, execution sensitivity, and fixed-parameter annual walk-forward
runs. These are research defaults, not evidence of profitability.

Execution is an approximation: strict price penetration and limited participation
do not establish queue priority. Historical risk sampling uses daily closes outside
entry windows; it cannot validate continuous five-second liquidation. The report
states these limitations. Zero fills do not validate execution. Hosting costs are
reported separately, including passive comparisons with and without the same charge.

## Equity data and cross-market research

Alpha Vantage ingestion uses `TIME_SERIES_DAILY_ADJUSTED` with `outputsize=full`.
This requires a premium-capable key according to the [provider documentation](https://www.alphavantage.co/documentation/).
Set `ALPHAVANTAGE_API_KEY` in your shell or protected service environment; do not
put the key in command arguments or this repository. Downloads are explicit,
one request per symbol, with no automatic retries on quota or entitlement errors.
SPY, QQQ and IWM are the initial broad-market, technology-heavy and small-cap
research proxies. Only US/Eastern listings are supported.

```bash
uv run liquidity-loop equity-ingest --symbols SPY QQQ IWM
uv run liquidity-loop equity-research --start 2022-03-01 --end 2024-01-01
uv run liquidity-loop backtest --product BTC-USD --start 2022-03-01 --end 2024-01-01 --equity-filter SPY
```

Ingest crypto daily history for each product over the requested dates first.
`equity-research` reads cached data only and compares all three crypto assets.
Its JSON reports live in `var/equity-reports/`, separately from strategy reports.
It tests prior 1/5/20 equity-session returns against subsequent 1/7 crypto-day
open-to-open returns and reports sample counts, missing-feature days, Pearson
correlations and conditional gross returns. Constant series have null correlations;
fewer than 30 pairs or missing crypto candles are labeled incomplete. Features
are usable from the next UTC midnight, carry over weekends, and expire after
four calendar days; gaps longer than four days invalidate the feature window.
Targets must finish inside the requested date partition.

The optional backtest filter fixes the rule in advance: allow the existing
breakout strategy when prior 20-session equity momentum is positive; otherwise
exit. Missing or stale features fail the run. This reuses minute execution, fees,
slippage, operating costs and the asset's buy-and-hold benchmark. Filtered strategy
reports include the equity rule and input hash and appear with the other backtests.
The BTC paper worker continues to use the existing crypto-only strategy.

Repeated weekend features and overlapping seven-day targets are dependent samples.
Correlations do not establish causality or net trading profits. Provider-adjusted
history is a current snapshot, not point-in-time vintages. Refresh ingestion to
capture revisions; revisions are recorded as events and reports hash their inputs.
Choose hypotheses using training data, inspect validation separately, and freeze
rules before evaluating a fresh untouched period. Existing 2024–2025 results have
already been inspected, so those dates cannot establish a newly selected strategy's
out-of-sample performance. Schema version 3 adds a separate equity cache and retains
existing candles, portfolios and reports.

## Offline market and macro archives

The user-supplied `backtesting/d_world_txt.zip` and `d_macro_txt.zip` can be read
directly, without extraction or an API key. This workflow hashes the archives and
selected members and leaves the Coinbase cache as the source of crypto returns.

```bash
uv run liquidity-loop archive-research --start 2019-01-01 --end 2022-01-01
uv run liquidity-loop archive-research --start 2022-03-01 --end 2024-01-01
uv run liquidity-loop archive-research --start 2024-01-01 --end 2026-01-01
uv run liquidity-loop backtest --product BTC-USD --start 2022-03-01 --end 2024-01-01 --market-filter '^NDQ'
```

`archive-research` is entirely offline and produces JSON plus readable Markdown
in `var/archive-reports/`. It tests the fixed `^NDQ`, `^NDX`, EURUSD, 2YUSY.B and
10YUSY.B series at 1/5/20 prior sessions against subsequent 1/7-day crypto returns.
Yield features are changes in quoted yield units, not investment returns. Missing
Coinbase periods are reported incomplete; SOL's early training is unavailable.
The fixed `--market-filter` supports either index and reuses the existing
20-session momentum rule and minute execution engine. Backtests can download
missing Coinbase execution windows; correlation research never downloads data.
Use `--market-archive` to specify another archive path for backtests.

Macro series are inventoried, with CPIYUS.M, UNRTUS.M, INRTUS.M and GDPQUS.M
coverage recorded in each report. They are excluded from predictive tests because
the files provide observation/reference dates without release timestamps or
historical vintages. A fixed arbitrary delay cannot establish that revised values
were available at a past decision. Archive crypto coverage is also recorded, but
those candles do not replace Coinbase execution data. Archive market close times
and price adjustments are not verified; next-UTC-day alignment is an explicit
research assumption. Previously inspected 2024–2025 results are descriptive, not
a fresh holdout for strategy selection.

See [archive research results](docs/ARCHIVE_RESEARCH_RESULTS.md) for the completed
correlation study, fixed-filter backtests, costed comparisons and remaining gaps.

## Intraday leading indicators

Research now includes spot/perpetual CVD divergence, pre-trigger open-interest
and funding crowding, and persistent ask-depth thinning for BTC, ETH and SOL.
These use completed five-minute observations and separate spot/perp sources.
Missing feeds remain unavailable rather than becoming neutral signals.

```bash
uv run liquidity-loop microstructure-download --month 2023-11
uv run liquidity-loop microstructure-research --input var/microstructure-data/cvd-2023-11.csv --start 2023-11-01 --end 2023-12-01
```

The explicit downloader verifies checksums for public Binance spot/perpetual
archives and derives signed aggressive volume from taker-buy quote volume.
It supplies CVD history; OI, funding and book data require additional inputs.
Research produces JSON and Markdown event studies in `var/microstructure-reports/`,
including coverage, delayed entry timing, nonoverlapping events and illustrative
cost sensitivities. These are event diagnostics, not portfolio backtest results.
No new trading signals are activated in the continuous paper worker.

See [microstructure definitions, data schema and initial evidence](docs/MICROSTRUCTURE_RESEARCH.md).
The initial November 2023 CVD study has small, mixed gross outcomes that do not
survive the current illustrative taker costs. OI/book historical performance is
unavailable; their detector logic is covered by tests.

## Coinbase CLI and credentials

CDP CLI setup follows [Coinbase's cdp-setup skill](https://docs.cdp.coinbase.com/cdp-cli/skill.md).
The CLI is an optional operator tool; the paper application uses public Advanced
Trade endpoints directly. CLI authentication is distinct from validation of retail
Advanced Trade account permissions and portfolio isolation for a future live adapter.

```bash
node --version                    # >=22
npm install -g @coinbase/cdp-cli
cdp --version
cdp env live --key-file /absolute/path/to/downloaded-key.json
cdp env
cdp evm accounts list             # Read-only authentication check
```

Create/download the API key using the Coinbase Developer Portal, store it outside
this repository, and restrict its permissions. Never paste secrets in chat. Use the
OS keychain rather than `--plaintext`. CDP commands may need elevated access from
Codex to access the keychain. Non-custodial wallet operations additionally need a
wallet secret; this BTC spot paper application does not use them or create wallets.

## Email and deployment

Optional Resend delivery reads `LOOP_RESEND_API_KEY`, `LOOP_EMAIL_FROM`, and
`LOOP_EMAIL_TO` from the process environment. A verified sender must be configured.
Without all three, alerts remain persisted and visible, with sending unconfigured.
Delivery retries independently of trading. Do not enable delivery until recipient
and sender are deliberately configured.

See [operations](docs/OPERATIONS.md) for private deployment, backups, and recovery,
and [readiness](docs/READINESS.md) for what has and has not been validated. The
[initial evidence](docs/INITIAL_RESULTS.md) records local checks and historical results. Cloud
purchases, production deployment, email delivery, and live enablement require
separate owner decisions. Paper engineering work continues even if backtests fail.

### Comparing assets

Research uses independent $500 portfolios and each asset's own buy-and-hold benchmark.
Select BTC, ETH or SOL in the dashboard research comparison to see its conclusions.
The common held-out period is January 2024 through December 2025. ETH uses the BTC
training and validation dates. SOL omits 2019–2021 training and the 2021 annual run;
its validation and 2022 annual run start March 1, 2022 to allow 250 days of warmup
after Coinbase's June 2021 listing. Those shorter runs are labeled in the reports.
No parameter selection uses held-out results. Fees and execution approximations
are shared assumptions; actual fees and liquidity may differ across assets.

Candle caches and data manifests identify the Coinbase product. Schema version 2
automatically copies legacy BTC candles into the asset-specific cache and retains
the legacy table. Reports without a product field are treated as BTC. This adds
historical testing; the continuous paper worker still operates BTC-USD.

See [multi-asset results](docs/MULTI_ASSET_RESULTS.md) for the completed ETH/SOL experiments and SOL data limitations.
