# Market archive research results

The supplied archives enable offline cross-market research without Alpha Vantage. A fixed positive 20-session ^NDQ momentum gate improved SOL in the two examined periods. It is an exploratory candidate, not a confirmed strategy.

The study reads archive members directly and preserves Coinbase crypto daily and minute data. Neither source archive nor the existing crypto cache was replaced.

## Data coverage

| Archive | Series | Relevant coverage |
| --- | ---: | --- |
| d_world_txt.zip | 2,426 | ^NDQ, ^NDX, EURUSD, US 2/10-year yields; BTC from 2010, ETH from 2015, SOL from April 2020 |
| d_macro_txt.zip | 826 | US inflation, unemployment, policy-rate and GDP series, plus other countries |

Both archives reach 2026. Reports retain archive/member hashes and observation coverage. Market adjustment methodology and original close timestamps remain unverified. Archive crypto coverage is informational; it does not supply executable minute data.

## Lagged correlations

Fixed tests use 1/5/20 prior market sessions against next 1/7-day Coinbase open-to-open returns. Index/FX features are returns; yield features are changes in quoted units. Features become usable the next UTC midnight, with stale values and data gaps excluded.

Training 2019–2021 produced 60 complete tests and one incomplete asset: Coinbase SOL cannot cover that full period. March 2022–December 2023 and 2024–2025 each produced 90 complete tests.

Predictive correlations are generally modest and change across periods. For BTC, ^NDQ 20-session momentum versus next-day returns has correlations -0.0133 (training), 0.0062 (validation), and 0.0082 (2024–2025). That does not indicate a useful linear next-day BTC predictor.

Seven-day targets overlap and weekend market features repeat. The largest correlation in a grid is not independent evidence; the study does not select rules by ranking correlations.

Full correlation tables:

- [Training](../var/archive-reports/archive-research-2019-01-01-2022-01-01.md)
- [Validation](../var/archive-reports/archive-research-2022-03-01-2024-01-01.md)
- [Previously inspected evaluation period](../var/archive-reports/archive-research-2024-01-01-2026-01-01.md)

## Fixed index filter backtests

Rule: apply the existing breakout strategy only when the previous 20-session ^NDQ return is positive; otherwise exit. This rule was fixed before inspecting these new results. Execution uses the existing minute simulation, spread, slippage, participation limits, maker/taker fees and asset-specific buy-and-hold benchmark.

All runs start with $500. Values below are ending liquidated wealth after fees/slippage and the same $5/month operating charge for each comparison. Each asset is an independent portfolio.

### March 2022–December 2023

| Asset | Existing strategy | ^NDQ filter | Buy-and-hold | Filter max drawdown |
| --- | ---: | ---: | ---: | ---: |
| BTC | $421.92 | [$316.50](../var/archive-backtests/equity-^NDQ-6c39e627-b534-434d-a95f-15bb0a1cc74c.json) | $369.37 | 24.22% |
| ETH | $394.12 | [$298.42](../var/archive-backtests/equity-^NDQ-9b5ed3e1-5fd0-4d93-af03-36ba3292fe40.json) | $272.21 | 23.11% |
| SOL | Incomplete | [$648.61](../var/archive-backtests/equity-^NDQ-ef3ae41f-3cd5-4fbd-b509-4fcd10c6aab6.json) | $389.76 | 26.06% |

### 2024–2025 (previously inspected)

| Asset | Existing strategy | ^NDQ filter | Buy-and-hold | Filter max drawdown |
| --- | ---: | ---: | ---: | ---: |
| BTC | $535.85 | [$657.75](../var/archive-backtests/equity-^NDQ-39607e17-4362-417c-86a5-3826080f84a0.json) | $893.42 | 17.48% |
| ETH | $605.56 | [$605.56](../var/archive-backtests/equity-^NDQ-e5f732b0-5466-476f-b22d-cf40b8593607.json) | $517.34 | 34.78% |
| SOL | $118.84 | [$524.48](../var/archive-backtests/equity-^NDQ-44de9232-8d8f-421d-b494-aa4ad68fad96.json) | $479.41 | 49.05% |

SOL validation without the filter is incomplete: Coinbase minute data at 2023-10-20 00:09 and 00:14 UTC are missing in the required entry window. The filtered run did not require that window. No candles were fabricated to complete the baseline. An identical gap was already recorded by the original research suite.

The filter worsened BTC and ETH versus their matched existing strategies in validation. In 2024–2025 it improved BTC but still lagged buy-and-hold, left ETH unchanged, and improved SOL over both the existing strategy and buy-and-hold. SOL is the candidate to investigate further; this does not establish a strategy that works for all three assets.

## Macro handling

CPIYUS.M, UNRTUS.M, INRTUS.M and GDPQUS.M were parsed and inventoried. They are excluded from predictive signals: the supplied files have observation/reference dates but no release timestamps or historical vintages. Revised macro values cannot be assumed to have been known on those dates. Release/vintage metadata is needed before macro trading tests are defensible.

## Validation and next experiment

58 automated tests pass; Ruff checks pass. Tests cover archive parsing, bad rows, ticker/date validation, safe member reads without extraction, time alignment, yield transforms, incomplete crypto coverage, exclusion of macro signals, and future-data invariance.

Do not tune the filter using the previously inspected 2024–2025 period. Freeze the SOL hypothesis, confirm archive provenance and timing, and test it on untouched data or a new paper-forward period. The current results are approximate candle execution evidence, not observed live fills.
