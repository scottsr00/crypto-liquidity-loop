# Intraday leading-indicator research

Three research families are implemented for BTC, ETH and SOL. They are fixed hypotheses, not claims that spot buying must beat derivatives positioning or that a crowded market must liquidate.

## Candidate definitions

| Family | Fixed initial rule | Intended use |
| --- | --- | --- |
| Spot/perp CVD | Prior 60-minute spot signed quote volume / total quote volume >= 10%; positive delta in each 20-minute third; price return between -1% and +0.2% | Candidate absorption alert |
| Perp-only rally | Price return > +0.2%; spot normalized delta <= 2%; perp normalized delta >= 10% | Entry-quality/risk warning |
| OI/funding crowding | Base-asset OI rises >= 5% in the preceding hour; high/low range <= 1%; settled funding scaled to 8h >= +0.03% with taker buy/sell >= 1.2, or <= -0.03% with ratio <= 0.8 | Crowding alert |
| OI trigger | A later completed 5-minute price move >= 0.2% against the crowded side | Potential short squeeze or long flush; not observed liquidation confirmation |
| Ask liquidity | Same-venue, complete 25-bps book band: ask quote depth falls >= 50% versus median preceding snapshots; bid/ask depth >= 2; spread <= 10bps; persists three 5-minute observations; positive spot flow | Candidate upward liquidity squeeze |

CVD change is the sum of aggressive buy quote volume minus aggressive sell quote volume. The spot and perpetual deltas are normalized separately by their own total volume; raw cumulative levels are not compared across venues. Flat or falling prices with aggressive buying are consistent with absorption, but do not identify the buyers or prove future price movement.

Base-unit OI avoids treating a price increase as growth in USD-valued OI. OI growth alone does not reveal leverage or which side will win. Funding and aggressive flow are positioning proxies. Order-book orders can be canceled; persistent band-depth measurements do not prove that a later buy order will occur.

## Reproduce the initial CVD study

```bash
uv run liquidity-loop microstructure-download --month 2023-11
uv run liquidity-loop microstructure-research --input var/microstructure-data/cvd-2023-11.csv --start 2023-11-01 --end 2023-12-01
```

The downloader obtains six monthly Binance spot/USD-M perpetual 5-minute kline archives and their SHA256 checksums. Both datasets contain taker-buy quote volume. Aggressive sell quote volume is total quote volume minus taker-buy quote volume, so each interval produces an exact signed-volume aggregate for that venue. The normalizer handles spot microsecond timestamps used from 2025 and older millisecond timestamps. The historical availability convention is interval end + one second; actual provider publication latency is unverified. The event study waits for a later full interval open.

Schema and time-unit reference: [Binance public data documentation](https://github.com/binance/binance-public-data). Public REST OI history covers only the latest month per [Binance market-data documentation](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data); longer OI and book studies need historical datasets or recorded forward data with known units, timestamps and coverage.

## November 2023 initial evidence

Each asset has 8,639 study intervals. These are previously inspected validation dates, not a fresh strategy holdout. The result is a one-month diagnostic study on Binance USDT prices, not the Coinbase USD portfolio simulator.

| Asset | Absorption events (60m) | Average subsequent spot return | All eligible intervals average | Illustrative long return after costs |
| --- | ---: | ---: | ---: | ---: |
| BTC | 37 | 0.0353% | 0.0131% | -1.9652% |
| ETH | 76 | 0.0282% | 0.0180% | -1.9721% |
| SOL | 42 | 0.1014% | 0.0702% | -1.9004% |

The positive averages are small and inconsistent across horizons. The perp-only rally warning also varies by asset: it preceded small negative BTC averages but positive ETH averages; SOL has only two events at each horizon, which is insufficient. These outcomes do not establish a reliable directional rule.

Cost sensitivity uses the current 0.9% taker fee on both legs, 2bps assumed full spread and 10bps slippage per leg. It excludes operating cost and does not simulate fills, queue priority or market impact. Every tested CVD long-event average is negative under these costs. Negative-direction events report subsequent spot returns, not short-selling profit.

- [Full event table](../var/microstructure-reports/microstructure-df7073f3-d551-40c5-bad7-74161b24b3f3.md)
- [JSON evidence and source manifests](../var/microstructure-reports/microstructure-df7073f3-d551-40c5-bad7-74161b24b3f3.json)

## Adding OI, funding and book history

The canonical input is a five-minute CSV; [the header template](microstructure-template.csv) lists all columns. Each row represents one completed interval for one asset.

- `asset`: BTC, ETH or SOL; `timestamp`: UTC interval-end epoch seconds; `available_at`: when every supplied value was knowable, never earlier than the interval end.
- `spot_venue`, `perp_venue`, `book_venue`: stable source identifiers. Units and instruments must stay fixed. The bundled downloader uses Binance USDT, not USD.
- `open`, `high`, `low`, `close`: spot OHLC for the completed interval.
- `spot_buy_quote`, `spot_sell_quote`, `perp_buy_quote`, `perp_sell_quote`: aggressive-side quote amounts in matching windows. If using trade tapes, check whether provider side means maker or taker before assigning buys/sells.
- `oi_base` and `oi_timestamp`: contemporaneous base-asset OI, with source observation time. Never substitute quote-notional OI or forward-fill it across missing intervals.
- `funding_rate`, `funding_interval_hours`, `funding_timestamp`: the latest known settled rate as a fraction, its actual payment interval, and its observed time. Future funding settlements are not permitted; a live predicted rate needs a separate provenance convention.
- `bid_depth_quote`, `ask_depth_quote`: summed price * quantity inside the same mid-relative band on both sides; `book_band_bps`: 25; `spread_bps`: full best-ask/best-bid spread relative to mid; `book_timestamp`: snapshot observation time.
- `spot_complete`, `perp_complete`, `book_complete`: literal true/false. Only mark true with full coverage of the relevant interval/band. Book replay must start from a snapshot and reject sequence gaps/reconnects until resynchronized.

Unavailable numeric/source-time values are empty, with completeness false. Missing input is unknown rather than zero. Duplicate intervals, future source timestamps, invalid prices and nonfinite quantities are rejected. Feature windows require continuous intervals and consistent venues. Stale OI/books and expired funding observations do not trigger signals.

The current downloader leaves OI, funding and book fields empty. Those families remain explicitly unavailable in the real-data report; their detector behavior is verified with controlled test cases. No historical OI or depth performance is claimed.

## Evaluation boundaries

The event study uses 15/60-minute later spot returns. Overlapping events are skipped within each signal/horizon. Baseline windows still overlap and signals share outcomes; sample counts do not justify independent-observation significance claims. The 30-event completeness threshold is a reporting convention, not statistical confidence.

Use these alerts as candidate entry/risk filters only after validating multiple chronological training/validation periods with frozen thresholds and matched fees/execution. Keep an untouched evaluation or new paper-forward period. The continuous worker remains on its existing strategy; these research alerts do not enable real orders, perpetual trading or shorts.

Validation covers exact taker-volume normalization, millisecond/microsecond timestamps, CVD divergence, pre-trigger OI/funding setup, funding interval scaling, depth persistence, source gaps, stale/missing observations, duplicate/future rows, nonoverlapping event windows, and entry timing after availability.
