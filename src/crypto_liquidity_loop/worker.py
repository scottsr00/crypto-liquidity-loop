import asyncio
import fcntl
import logging
import time
import uuid

from .coinbase import CoinbasePublic, QuoteStream
from .domain import DAY, signal
from .execution import PaperEngine
from .notifications import EmailDelivery

log = logging.getLogger(__name__)


class WorkerLock:
    def __init__(self, store):
        self.store = store
        self.owner = str(uuid.uuid4())
        self.file = None

    def __enter__(self):
        self.file = open(str(self.store.path) + ".lockfile", "a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError("Another worker already owns this paper portfolio") from None
        with self.store.transaction() as db:
            # Process lock is authoritative on this single-host deployment; takeover is audited.
            previous = db.execute("SELECT owner FROM ownership WHERE id=1").fetchone()
            db.execute("INSERT OR REPLACE INTO ownership VALUES(1,?,?)", (self.owner, time.time()))
            self.store.event(
                db,
                "worker_acquired",
                {"owner": self.owner, "previous": previous[0] if previous else None},
            )
        return self

    def heartbeat(self, now):
        with self.store.transaction() as db:
            cursor = db.execute(
                "UPDATE ownership SET heartbeat=? WHERE id=1 AND owner=?", (now, self.owner)
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Worker ownership lost")

    def __exit__(self, *_):
        with self.store.transaction() as db:
            db.execute("DELETE FROM ownership WHERE id=1 AND owner=?", (self.owner,))
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


async def run_worker(store, config):
    with WorkerLock(store) as lock:
        client = CoinbasePublic()
        # Independent tasks keep history downloads and email outside the risk loop.
        stream = QuoteStream()
        stream_task = asyncio.create_task(stream.run())
        product_task = asyncio.create_task(asyncio.to_thread(client.product))
        history_task = None
        recovery_task = None
        notification_task = None
        engine, decision, rest_quote = None, None, None
        history_day, history_retry, reconcile_at = None, 0, 0
        try:
            while True:
                started = time.monotonic()
                now = time.time()
                lock.heartbeat(now)
                if engine is None:
                    if product_task.done():
                        try:
                            engine = PaperEngine(store, config, product_task.result())
                        except Exception as exc:
                            log.warning("Product metadata unavailable: %s", type(exc).__name__)
                            product_task = asyncio.create_task(asyncio.to_thread(client.product))
                    if engine is None:
                        await asyncio.sleep(config.poll_seconds)
                        continue
                if now >= reconcile_at:
                    engine.reconcile(now)
                    reconcile_at = now + 60
                day = int(now) // DAY * DAY
                needed = max(config.sma_days, config.entry_days + 1, config.exit_days + 1)
                if history_task is None and history_day != day and now >= history_retry:
                    history_task = asyncio.create_task(
                        asyncio.to_thread(client.candles, day - needed * DAY, day)
                    )
                if history_task is not None and history_task.done():
                    try:
                        candles = history_task.result()
                        store.save_candles(candles, DAY)
                        decision = signal(candles, config)
                        history_day = decision["timestamp"]
                    except Exception as exc:
                        history_retry = now + 30
                        with store.transaction() as db:
                            store.alert(
                                db,
                                "candles",
                                "Daily candle data unavailable",
                                f"Integrity or connectivity check failed ({type(exc).__name__}); signals blocked.",
                                now,
                            )
                        decision = None
                    history_task = None
                quote = stream.latest
                if quote is None or not quote.fresh(now, config.quote_max_age_seconds):
                    quote = rest_quote
                    if recovery_task is None:
                        recovery_task = asyncio.create_task(asyncio.to_thread(client.quote))
                if recovery_task is not None and recovery_task.done():
                    try:
                        rest_quote = recovery_task.result()
                        if rest_quote.fresh(now, config.quote_max_age_seconds):
                            quote = rest_quote
                    except Exception as exc:
                        log.warning("REST quote recovery failed: %s", type(exc).__name__)
                    recovery_task = None
                # Old decisions may be displayed but cannot open new entry windows after midnight.
                current_decision = decision if decision and decision["timestamp"] == day else None
                engine.tick(
                    quote,
                    now,
                    current_decision,
                    connectivity={
                        "quote_source": "websocket"
                        if quote is stream.latest and quote is not None
                        else "rest",
                        "websocket_error": stream.error,
                        "daily_candle_close": history_day,
                    },
                )
                if notification_task is None or notification_task.done():
                    if notification_task is not None:
                        notification_task.result()
                    notification_task = asyncio.create_task(
                        asyncio.to_thread(EmailDelivery(store).deliver_once)
                    )
                await asyncio.sleep(max(0, config.poll_seconds - (time.monotonic() - started)))
        finally:
            tasks = [
                t
                for t in (stream_task, product_task, history_task, recovery_task, notification_task)
                if t
            ]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            client.close()
