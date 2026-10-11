# Multi-asset research results

Held-out period: January 1, 2024 through December 31, 2025. Independent $500 portfolios, fixed 55/20/200 strategy, shared modeled trading costs. Operating costs excluded below.

| Asset | Strategy wealth | Own buy-and-hold | Difference | Completed positions | Wins / losses |
| --- | ---: | ---: | ---: | ---: | --- |
| BTC | $655.94 | $1,023.73 | $-367.80 | 5 | 3 / 2 |
| ETH | $725.65 | $643.85 | $81.80 | 5 | 3 / 2 |
| SOL | $238.92 | $605.55 | $-366.62 | 6 | 2 / 4 |

ETH outperformed its benchmark in this held-out test. BTC and SOL underperformed their own benchmarks; SOL also lost capital. SOL triggered the 70% drawdown halt: sampled maximum drawdown was 71.63%, measured from its portfolio peak. Its position was liquidated and new entries stopped for the rest of the run. SOL buy-and-hold sampled maximum drawdown was 59.79%. These results cover one strategy on three assets and do not establish performance of other crypto strategies.

ETH: all 11 planned tests completed. SOL: five tests completed; four were rejected as incomplete (main validation, both execution sensitivity tests, annual 2023). Coinbase omitted two required minute candles on October 20, 2023, at 00:09 and 00:14 UTC. Missing tradable prices were not filled or inferred. SOL parameter variants had different execution windows and passed the required-data checks.

SOL training 2019–2021 and annual 2021 were omitted due to listing history. Validation and annual 2022 start March 1, 2022 for 250-day warmup. Only held-out dates are identical across all three assets. No parameters were selected using held-out outcomes.

Reports are stored in SQLite and var/reports. Exported summaries: RESEARCH_SUMMARY.md, RESEARCH_SUMMARY_ETH.md, RESEARCH_SUMMARY_SOL.md. The dashboard offers per-asset explanations, completed trades, period results, and incomplete-run notices.

Execution remains approximate: OHLC minute data cannot establish real queue priority or fills; spread, slippage, and fees are shared assumptions. Current exchange increments are applied historically. Continuous paper trading remains BTC-USD; live order execution is disabled.
