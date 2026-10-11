# Readiness and evidence

## Implemented scope

Public BTC-USD historical ingestion, shared pure strategy/accounting/risk calculations,
candle backtests with minute execution windows, a persistent paper execution engine,
WebSocket quote monitoring with REST recovery, owner dashboard and audited controls,
optional Resend outbox, process supervision examples, and SQLite backup support.

The CLI accepts only paper mode. No authenticated retail exchange order adapter,
live enablement control, account transfer, or withdrawal capability exists.

## Coinbase CLI setup

CDP CLI 2.0.109 is installed and its `live` environment is authenticated using the
owner's downloaded Secret API Key, imported through the CLI's OS keychain storage.
A read-only `cdp evm accounts list` request succeeded and returned an empty account
list. No wallets were created or funded. No wallet secret was configured; paper
trading does not require one. Secret contents are not stored in this repository.

This verifies CDP platform authentication, not retail Advanced Trade account access,
trade permissions, or portfolio isolation.

## Evidence required outside a local implementation session

- A continuous 2–4 week paper observation period, extended when signals are sparse.
- Production reboot, private network/firewall, clock synchronization, durable storage,
  backup schedule, and off-host restore verification.
- Verified sender and recipient, provider account configuration, and observed inbox delivery.
- Before any live work: account/regional availability, current actual fees, isolated
  retail portfolio permissions, cancellation and submission-timeout behavior, and
  explicit owner authorization. No live credentials are needed for paper simulations.

Historical OHLC execution remains approximate, and simulated orders do not reproduce
exchange queue priority or API submission/cancellation races. Local tests exercise
durability, uncertain-state blocking, partial fills, deadline recovery, and ledger
invariants; they do not claim validation of a real exchange execution adapter.

Research periods and parameter scenarios are defined before results are inspected.
Unfavorable results must be reported, while the paper application still ships. No
automated tuning or progression to live follows from any favorable report.
