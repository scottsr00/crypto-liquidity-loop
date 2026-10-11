# Operations

## Deployment preparation

Use one Linux VM, a dedicated unprivileged `liquidity-loop` service user, Python 3.12,
and two supervised processes. Default provider remains DigitalOcean; verify current
regional pricing and Tailscale eligibility before purchase. No cloud resources are
created by this repository. Keep development and production databases separate.

Install the checkout at `/opt/liquidity-loop`, run `uv sync --frozen --no-dev`, and put
configuration at `/etc/liquidity-loop/config.toml` with database
`/var/lib/liquidity-loop/paper.sqlite3`. Give the service user ownership of that data
directory. Put secrets in `/etc/liquidity-loop/secrets.env`, owned by root with mode
0600, loaded by systemd; exclude it from backups. The worker needs only public data.

Use the unit files in `scripts/systemd/` after adjusting paths and user names. Enable
clock synchronization and confirm UTC candle boundaries. Restrict inbound traffic
to SSH through the private network; the dashboard binds to loopback. Access it through
an SSH tunnel over Tailscale, or bind explicitly to its Tailscale IPv4 address with
a configured owner token. Do not expose port 8000 publicly. Owner sessions expire
after 12 hours; controls require CSRF tokens. Liquidation and peak reset require
separate confirmation. No live enablement endpoint exists.

## Readiness checks

1. `liquidity-loop --config /etc/liquidity-loop/config.toml doctor` checks named bindings
   by presence without displaying secrets.
2. Start the worker, confirm fresh quotes and successful reconciliation on the dashboard.
3. Confirm orders and cash agree with fills. A startup accounting mismatch blocks fills
   and new strategy orders; resolve it before resuming.
4. Configure a verified sender and recipient before enabling email. An inbox receipt
   must be checked by the owner; provider acceptance alone does not prove delivery.
5. Observe reboot recovery, disconnection, and backup restore on paper funds before
   considering production readiness. Record controlled scenarios separately from
   natural market signals. Observe at least 2–4 weeks and extend for sparse signals.

## Backup and restore

```bash
liquidity-loop --config /etc/liquidity-loop/config.toml backup /path/to/new-backup.sqlite3
```

Use SQLite's online backup API, not a copy of the main file while WAL writes are active.
Back up daily to private durable storage; retain seven daily and four weekly copies.
Backups include state and trading history but no API or dashboard secret values.
Protect backup access and verify `PRAGMA integrity_check` after creation.

To restore, stop both services, retain the current database for investigation, restore
the backup to a **new** data path, and update configuration to that path. Check database
integrity and configuration fingerprint. Start the worker first and confirm ledger
reconciliation before starting the dashboard. Never merge histories from different runs.
Restoring paper state loses observations since backup; this is not a safe live exchange
restore procedure. Any future live adapter must recover exchange orders and fills first.

## Incident response

- Stale quotes: entries and fills are blocked; order deadlines still expire. The worker
  attempts fresh REST quotes independently of daily candle ingestion. Check connectivity
  and timestamps. Do not use an old valuation as a current drawdown measurement.
- Drawdown halt: halt is saved first; the next fresh, reconciled tick sells simulated BTC.
  Limited visible depth can leave a partial liquidation. Below-minimum dust remains
  visible and produces a deduplicated alert. Halts never clear automatically.
- Pause entries: cancel pending buys and retain partial fills. Exit rules and risk checks
  continue. Liquidate-and-pause instead sells holdings using taker assumptions.
- Resume: fresh quotes and reconciliation are required. Resume is rejected while
  liquidation is incomplete or drawdown still meets 70%. Resetting the peak is a distinct
  audited action; it does not itself resume trading.
- Worker failure: use systemd restart, review heartbeat and audit events, and confirm
  startup reconciliation. A process lock and persisted owner prevent two workers.
- Email failure: delivery retries with capped backoff and stops after 20 attempts;
  trading continues. Review persisted delivery errors, which contain exception classes
  rather than credentials or provider response bodies.

Use journald with bounded retention (for example `SystemMaxUse=200M`) and monitor disk
space. SQLite stores five-second valuations; schedule retention or archive old runs
before disk usage becomes material. Keep local data and reports outside version control.
