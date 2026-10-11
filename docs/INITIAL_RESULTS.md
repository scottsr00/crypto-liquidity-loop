# Initial local evidence — October 10, 2026

The first fixed research suite completed using public Coinbase BTC-USD daily candles
and minute execution windows. Starting capital was $500 in each independent period.
The default 55/20/200 strategy and 0.5% maker / 0.9% taker fee assumptions were retained.

| Period | Strategy ending wealth | BTC buy-and-hold | Strategy max drawdown | Fills |
| --- | ---: | ---: | ---: | ---: |
| Training: 2019–2021 | $2,660.20 | $6,195.69 | 43.21% | 12 |
| Validation: 2022–2023 | $537.51 | $452.60 | 28.73% | 9 |
| Held-out: 2024–2025 | $655.94 | $1,023.73 | 21.71% | 10 |

These ending values include simulated trading fees and execution costs, before
operating costs. At an assumed $5/month, held-out strategy wealth after operating
costs was $535.85. Held-out buy-and-hold maximum drawdown was 32.10%, compared with
21.71% for the strategy at the sampled valuations.

The strategy underperformed buy-and-hold on held-out ending wealth. No parameter was
selected using that result. Paper development continues as agreed; live remains disabled.
Validation sensitivity and annual fixed-parameter walk-forward reports are available in
`var/reports/RESEARCH_SUMMARY.md`, with full JSON reports and data input hashes. These
local artifacts are excluded from version control; use the backup procedure to retain them.

## Limits

Minute OHLC data cannot establish maker queue priority. Fills require strict price
penetration and are capped by assumed volume participation. Spread, slippage, and
historical product increments are approximations. Risk uses daily closes outside
entry execution windows, so these drawdowns and historical halts do not establish
continuous intraday liquidation performance. A zero-fill sensitivity scenario is
reported as such and does not validate execution.

## Local engineering checks

- 35 automated tests passed, covering signals, accounting, sizing, paper order recovery,
  expiry, partial fills and liquidation, durable halt, controls, email failure isolation,
  duplicate-worker prevention, candle pagination, backup restore, backtest causality,
  and WebSocket sequence validation including subscription acknowledgments.
- Ruff checks passed; dependencies are recorded in `uv.lock`.
- Actual public Coinbase REST history and product requests succeeded.
- The paper worker reached a healthy state with streaming WebSocket quotes after
  configuring verified CA certificates and allowing a bounded 32 MiB book snapshot.
- The private dashboard rendered over local HTTP. Owner controls default to disabled
  until an owner token is configured; the automated checks exercise authenticated controls.

No real orders, wallet funding, cloud purchases, production deployment, or email to an
actual recipient were performed. CDP CLI 2.0.109 was installed; key authentication is
pending the owner's downloaded Secret API Key JSON file path. This brief local run does
not satisfy the required multiweek paper observation period.
