import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candles (
  granularity INTEGER NOT NULL, start INTEGER NOT NULL, body TEXT NOT NULL,
  source TEXT NOT NULL, fetched_at REAL NOT NULL, PRIMARY KEY(granularity,start));
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fills (
  id TEXT PRIMARY KEY, order_id TEXT NOT NULL, timestamp REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS valuations (timestamp REAL PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS controls (
  id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, action TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', result TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, dedupe TEXT NOT NULL, subject TEXT NOT NULL,
  body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt REAL NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, timestamp REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ownership (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT, heartbeat REAL);
CREATE INDEX IF NOT EXISTS alert_dedupe ON alerts(dedupe,timestamp);
PRAGMA user_version=1;
"""


def encode(value):
    return json.dumps(value, default=str, separators=(",", ":"))


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, mode=0o700)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise ValueError("Unsupported database schema version")
            db.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                db.executescript(SCHEMA)
        with self.transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 1:
                db.execute(
                    "CREATE TABLE market_candles (product_id TEXT NOT NULL, "
                    "granularity INTEGER NOT NULL,start INTEGER NOT NULL,body TEXT NOT NULL,"
                    "source TEXT NOT NULL,fetched_at REAL NOT NULL,"
                    "PRIMARY KEY(product_id,granularity,start))"
                )
                # All pre-v2 candles are BTC. Retain the legacy table for recovery.
                db.execute(
                    "INSERT INTO market_candles SELECT 'BTC-USD',granularity,start,body,"
                    "source,fetched_at FROM candles"
                )
                db.execute("PRAGMA user_version=2")
            if db.execute("PRAGMA user_version").fetchone()[0] == 2:
                db.execute(
                    "CREATE TABLE equity_daily (symbol TEXT NOT NULL, session_date TEXT NOT NULL, "
                    "body TEXT NOT NULL, fetched_at REAL NOT NULL, "
                    "PRIMARY KEY(symbol,session_date))"
                )
                db.execute("PRAGMA user_version=3")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def initialize(self, config):
        with self.transaction() as db:
            state = self.state(db)
            if state is not None:
                if state["config_fingerprint"] != config.fingerprint():
                    raise ValueError("Settings differ from the persisted run; use a new database")
                return
            self.save_state(
                db,
                {
                    "mode": "paper",
                    "cash": str(config.initial_cash),
                    "btc": "0",
                    "peak": str(config.initial_cash),
                    "fees": "0",
                    "halted": False,
                    "paused": False,
                    "liquidating": False,
                    "last_signal": None,
                    "started_at": time.time(),
                    "benchmark_btc": None,
                    "benchmark_cash": "0",
                    "config_fingerprint": config.fingerprint(),
                    "settings": config.public(),
                    "quote": None,
                    "health": "starting",
                    "reconciled": False,
                },
            )

    def state(self, db=None):
        if db is None:
            with self.connect() as conn:
                return self.state(conn)
        row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save_state(self, db, state):
        db.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (encode(state),))

    def event(self, db, kind, body, now=None):
        db.execute(
            "INSERT INTO events(timestamp,kind,body) VALUES(?,?,?)",
            (time.time() if now is None else now, kind, encode(body)),
        )

    def alert(self, db, key, subject, body, now):
        if db.execute(
            "SELECT 1 FROM alerts WHERE dedupe=? AND timestamp>?", (key, now - 3600)
        ).fetchone():
            return
        db.execute(
            "INSERT INTO alerts(timestamp,dedupe,subject,body) VALUES(?,?,?,?)",
            (now, key, subject, body),
        )

    def save_candles(
        self, candles, step, source="Coinbase Advanced Trade public REST", product_id="BTC-USD"
    ):
        if product_id not in ("BTC-USD", "ETH-USD", "SOL-USD"):
            raise ValueError("Unsupported candle product")
        with self.transaction() as db:
            for c in candles:
                body = encode({k: getattr(c, k) for k in c.__dataclass_fields__})
                row = db.execute(
                    "SELECT body FROM market_candles WHERE product_id=? AND granularity=? AND start=?",
                    (product_id, step, c.start),
                ).fetchone()
                if row and row[0] != body:
                    self.event(
                        db,
                        "candle_revision",
                        {"product_id": product_id, "step": step, "start": c.start},
                    )
                db.execute(
                    "INSERT OR REPLACE INTO market_candles VALUES(?,?,?,?,?,?)",
                    (product_id, step, c.start, body, source, time.time()),
                )

    def candles(self, start, end, step=86400, product_id="BTC-USD"):
        from .domain import Candle, D

        with self.connect() as db:
            rows = db.execute(
                "SELECT body FROM market_candles WHERE product_id=? AND granularity=? AND start>=? "
                "AND start<? ORDER BY start",
                (product_id, step, start, end),
            ).fetchall()
        result = []
        for row in rows:
            data = json.loads(row[0])
            result.append(Candle(int(data.pop("start")), **{k: D(v) for k, v in data.items()}))
        return result

    def request_control(self, action):
        if action not in ("pause", "resume", "liquidate", "reset_peak"):
            raise ValueError("Unsupported control")
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT INTO controls(timestamp,action) VALUES(?,?)", (time.time(), action)
            )
            self.event(db, "control_requested", {"id": cursor.lastrowid, "action": action})
            return cursor.lastrowid

    def rows(self, table, limit=100):
        if table not in {
            "orders",
            "fills",
            "events",
            "valuations",
            "controls",
            "alerts",
            "reports",
        }:
            raise ValueError("Unsupported table")
        sort = "rowid" if table == "orders" else "timestamp"
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(f"SELECT * FROM {table} ORDER BY {sort} DESC LIMIT ?", (limit,))
            ]

    def backup(self, target):
        target = Path(target).resolve()
        if target == self.path or target.exists():
            raise ValueError("Backup target must be a new file")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        target.chmod(0o600)
