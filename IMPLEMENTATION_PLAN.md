# Crypto Liquidity Loop implementation plan

## Goal and scope

Build a single-user application that tests whether a low-turnover BTC spot strategy can outperform BTC buy-and-hold after fees. Start with $500 in simulated USD cash. Deliver backtesting and continuous paper trading before enabling real orders.

The first release trades BTC-USD only through Coinbase Advanced Trade. It uses no leverage, short selling, ETH, grid trading, or multiple strategies. Live trading requires the owner's explicit review and enablement; completing paper trading must never enable it automatically.

## Agreed trading rules

| Component | Specification |
| --- | --- |
| Signal schedule | Evaluate completed daily candles, using UTC day boundaries |
| Entry | Daily close strictly exceeds the highest high of the previous 55 completed daily candles and the 200-day simple moving average |
| Moving average | Includes the just-completed signal candle; requires 200 completed candles |
| Normal exit | Daily close strictly falls below the lowest low of the previous 20 completed daily candles |
| Position sizing | Invest available USD, reserving enough for fees, exchange increments, and minimum order requirements |
| Entry execution | Post-only limit order; cancel unfilled remainder after one hour; retain partial fills; no taker fallback |
| Normal exit execution | Limit-order attempt for one minute, then cancel and reconcile before submitting a taker order for the remaining BTC |
| Drawdown halt | At a 70% decline from peak portfolio value, cancel outstanding orders, reconcile, sell BTC with taker execution, and pause |
| Resume | Manual only; never clear a halt automatically |
| Fee assumptions | Maker 0.5%, taker 0.9%; record actual fees when available |
| Monitoring | Drawdown, order state, and data freshness monitored independently of daily signals |

The channel calculations exclude the signal candle. Comparisons are strict; equality does not trigger a trade. Trades occur after a candle closes, never at a price assumed available before its signal exists.

The 55/20/200 parameters are initial hypotheses, not proven settings. A 70% drawdown is an agreed tolerance and halt trigger, not a guaranteed loss ceiling.

## Decisions to resolve during implementation

Use these provisional defaults for paper trading and make them visible in configuration. Validate them against Coinbase capabilities before live trading:

- Place entry limits at the best bid; calculate the entry deadline from the daily signal evaluation, so order replacements cannot extend the one-hour window. Skip expired signals after downtime.
- Use a post-only ask-side limit for the normal exit attempt. Its one-minute deadline includes replacements; use supported market execution for the remainder afterward.
- Monitor risk at least every five seconds using fresh executable bid prices. Mark portfolio value as USD plus BTC valued at bid; include paid fees in cash accounting. Show estimated liquidation costs separately.
- If market data becomes stale, block new entries and alert. Do not calculate a trustworthy drawdown from stale prices; attempt a fresh REST quote and reconcile before acting. Exchange outages can prevent liquidation.
- Keep the portfolio peak persisted across restarts and manual resumes. Resetting the peak must be a separate explicit action with an audit record. Initially support no deposits or withdrawals during a run; block or flag unexplained balance changes rather than treating them as trading performance.
- A retained partial entry is a held position: do not top it up or pyramid. A completely unfilled entry may be reconsidered only at the next daily evaluation.
- Treat the dedicated Coinbase portfolio's BTC and USD as bot capital. Unexpected balances or ambiguous pending orders require reconciliation before trading.

## Architecture

Use Python, FastAPI, a simple server-rendered dashboard, and SQLite. Avoid a separate frontend toolchain and managed database for the first version. Pin a supported Python version and dependencies when implementation starts.

Run two supervised processes on one small Linux cloud VM:

1. A single bot worker owns strategy evaluation, exchange access, execution, reconciliation, risk monitoring, and notifications.
2. A dashboard process reads state and submits persisted control requests. It never places orders directly.

Backtesting runs as an explicit job, separate from the live worker. Prevent concurrent workers from controlling the same portfolio using a process lock and persisted ownership checks. Use SQLite WAL mode with short transactions and tested concurrency handling.

Suggested repository structure:

```text
src/crypto_liquidity_loop/
  config.py
  market_data/
  strategy/
  portfolio/
  execution/
  coinbase/
  backtest/
  risk/
  notifications/
  dashboard/
tests/
scripts/
docs/
```

Share pure signal, accounting, and risk logic across backtest, paper, and live modes. Keep execution adapters distinct so a simulated order cannot accidentally become a real one. Live mode requires explicit configuration plus a deliberate owner action; the default is paper mode.

## Coinbase integration and persistent state

- Confirm current Advanced Trade product metadata, supported daily candle boundaries, historical data coverage, fee information, portfolio permissions, and order behavior against official documentation.
- Use REST for historical candles, balances, order submission, cancellation, and reconciliation. Use supported WebSocket channels for quotes and order updates, with REST recovery after disconnects.
- Validate BTC-USD increments, minimum order size, available funds, and fees before submitting any order. Use decimal arithmetic for money and quantities.
- Persist candles, strategy decisions, portfolio valuations, peak equity, orders, fills, control requests, alerts, and audit events. Version the schema with migrations.
- Persist order intent and its unique client order ID before submission. After a timeout, query exchange state before retrying; do not assume an unsuccessful response means no order exists.
- Model pending submission, open, partial fill, cancellation pending, filled, rejected, and uncertain states explicitly. Never submit a replacement sell until cancellation and fills have been reconciled.
- Reconcile balances and open/recent orders at startup and periodically. Do not place new orders while reconciliation is incomplete.
- Persist halt state before liquidation begins. If liquidation fails or is partial, remain halted, report residual BTC, and retry only after checking exchange state.

## Dashboard and owner controls

Show a prominent backtest/paper/live mode indicator and data freshness timestamp on every operational page.

Provide:

- Portfolio value, USD balance, BTC quantity, peak value, drawdown, and estimated liquidation value.
- Performance against a matched BTC buy-and-hold benchmark, with trading-cost and hosting-cost views.
- Current signal, candle timestamp, strategy parameters, and next evaluation time.
- Orders, partial fills, trades, fee totals, and execution errors.
- Worker health, Coinbase connectivity, reconciliation status, and email delivery status.
- Backtest reports with period, data provenance, parameters, assumptions, and out-of-sample results.
- Pause entries, manually liquidate and pause, and resume controls. Clearly distinguish pausing entries from selling holdings; risk monitoring continues during an entry pause.

Restrict access to the owner through a private network such as Tailscale. Bind the app to loopback or the private interface and restrict firewall access. Protect control requests against CSRF, require confirmation for liquidation and live enablement, and audit all actions. Verify current Tailscale eligibility and pricing before deployment.

## Email alerts

Deliver alerts to the owner's personal Gmail inbox using a transactional email provider such as Resend or Postmark. Select the provider during integration based on sender verification requirements and current pricing. Production sending may require a verified domain; if unavailable, evaluate a supported Gmail OAuth or app-password sender instead.

Send order-fill summaries, execution failures, stale-data/connectivity alerts, drawdown-halt events, and recovery notifications. Deduplicate noisy errors, retry with backoff, and persist delivery status. Email failures must never block a trade or risk action. Do not include credentials or sensitive account details in messages.

## Backtesting and evidence

1. Acquire BTC-USD history with enough pre-period data for indicator warmup. Identify gaps, duplicates, timestamps, and any provider limits; reject incomplete runs rather than silently forward-filling tradable candles.
2. Use daily data for signals. Obtain finer data for the one-hour entry and one-minute exit policies, subject to source availability and cost. Daily OHLC alone cannot establish those fills.
3. If only candle data is available, label execution results as approximations. Use conservative fill assumptions and sensitivity scenarios; a price touching a limit does not guarantee a fill or its quantity. Live paper data will provide additional evidence, though simulated fills still cannot prove queue priority.
4. Charge maker/taker fees per fill, model spread and slippage, respect partial fills and minimum sizes, and prohibit look-ahead.
5. Invest the benchmark's $500 in BTC at the first tradable evaluation timestamp after warmup, including its entry fee. Use the same period and valuation convention for both. Also compare liquidated ending values with exit costs charged to both.
6. Report ending wealth, total return, annualized return where meaningful, maximum drawdown, time in cash, turnover, trades, missed entries, total fees, and risk-adjusted metrics with stated definitions. Never treat a zero-trade run as execution validation.
7. Separate training, validation, and held-out periods chronologically. Use walk-forward analysis and nearby parameter settings, including slower settings, without selecting parameters on the final held-out period.
8. Report strategy performance after trading costs and separately after actual or assumed hosting and email costs. Charge shared operating costs consistently when comparing the bot's economic outcome with passive ownership.

At $500, a $5 monthly server costs $60 per year, or 12% of initial capital. Infrastructure cost can outweigh trading gains and must remain visible.

## Delivery milestones and acceptance gates

### 1. Project foundation and historical data

Create the Python package, pinned dependency setup, configuration validation, database migrations, and data ingestion. Add repeatable development commands and secret-free example configuration.

Acceptance: a clean install succeeds; ingestion is repeatable; candle integrity checks catch known gaps; no real-order path is enabled.

### 2. Strategy and backtest

Implement deterministic signals, portfolio accounting, execution simulation, benchmarks, and report generation.

Acceptance: tests catch channel look-ahead, premature fills, incorrect fees, double-counted balances, and drawdown errors. Reports identify execution approximations and show actual trades when the selected period produces signals. Complete out-of-sample and parameter-neighborhood analysis before deciding whether the strategy merits paper deployment.

### 3. Paper worker and reconciliation

Connect live Coinbase data to simulated orders; implement persistent order state, expiry, partial fills, restart reconciliation, and the risk halt.

Acceptance: exercise disconnects, stale data, process restarts, lost submission responses, cancellation races, and partial liquidation. Verify no duplicate orders, durable halt state, and accurate residual balances. No trade-permitted Coinbase credential is required for simulated orders.

### 4. Dashboard and email

Build the owner views, protected controls, audit history, and notification delivery.

Acceptance: views match stored accounting; pause/resume and liquidation semantics work; unauthorized control requests fail; test email delivery reaches the configured inbox; provider failures do not block risk handling.

### 5. Cloud deployment and continuous paper run

Deploy to a small DigitalOcean VM as the default, with Hetzner or AWS Lightsail as alternatives. Budget approximately $5–10/month and verify current regional pricing before purchase. Use persistent storage, automatic service restarts, restricted access, log rotation, clock synchronization, and database backups with a tested restore procedure.

Acceptance: the system restarts safely after reboot, reconciles before trading, and restores state from backup. Run paper mode for at least 2–4 weeks, extending the period if there are too few natural signals. Supplement sparse trading with controlled execution scenarios; distinguish these from observed market results.

### 6. Live readiness review, then optional small live trading

Present backtest and paper evidence, remaining execution uncertainty, total costs, and proposed live settings. Seek explicit owner authorization before enabling live orders or purchasing hosting/provider services.

Use a dedicated Coinbase portfolio and keys scoped to required view/trade permissions with no transfers or withdrawals, where supported. Verify portfolio isolation and supported IP restrictions. Coinbase signing may require a real local private key: use an appropriate secure runtime secret store, not an assumed proxy placeholder. Never commit secrets or log their values.

Live acceptance: real fills and fees reconcile with balances; expiry and exit policies match tested behavior; halt and email mechanisms operate; any behavior or performance mismatch is reviewed before continuing or scaling. Passing engineering checks does not establish that the strategy beats buy-and-hold.

## Operational safeguards and deployment prerequisites

- Store secrets outside the checkout with restricted access; redact logs and exclude secrets from backups.
- Check required bindings by name and presence before requesting credentials. Do not request secret values in chat.
- Confirm Coinbase regional/account availability, API access, current fees, and usable portfolio isolation before live integration.
- Choose and configure a sender identity, email credentials, and the recipient securely; do not assume a personal Gmail inbox provides outbound API credentials.
- Document service installation, startup/readiness checks, backup/restore, incident response, and manual recovery.
- Use the existing isolated checkout during cloud development; do not create a Git worktree unless requested.
- Separate the development environment from production: production runs continuously, while development tasks may stop or restart.

## Completion criteria

The first release is complete when reproducible backtests, a validated paper worker, the private dashboard, email notifications, deployment instructions, and operating procedures exist and pass their relevant checks. Report profitability evidence and engineering readiness separately. If the strategy fails the buy-and-hold comparison, report that outcome and keep live trading disabled rather than tuning indefinitely until a favorable result appears.

This document is an implementation plan. No application, backtest results, production deployment, or live trading capability has been created or verified yet.
