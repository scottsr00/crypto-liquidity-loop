"""Public-only Coinbase adapter. Intentionally exposes no order submission methods."""

import asyncio
import json
import ssl
import time
from datetime import datetime

import certifi
import httpx
from websockets.asyncio.client import connect

from .domain import Candle, D, Product, Quote, validate_candles


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def advance_sequence(previous, message):
    value = message.get("sequence_num")
    if value is None:
        return previous
    if previous is not None and value != previous + 1:
        raise ValueError(f"Coinbase sequence gap: expected {previous + 1}, received {value}")
    return value


class CoinbasePublic:
    base_url = "https://api.coinbase.com/api/v3/brokerage/market"

    def __init__(self, transport=None, product_id="BTC-USD"):
        if product_id not in ("BTC-USD", "ETH-USD", "SOL-USD"):
            raise ValueError("Supported research products: BTC-USD, ETH-USD, SOL-USD")
        self.product_id = product_id
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=12,
            headers={"Cache-Control": "no-cache"},
            transport=transport,
        )

    def close(self):
        self.client.close()

    def _get(self, path, params=None):
        for attempt in range(4):
            try:
                response = self.client.get(path, params=params)
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (
                    429,
                    500,
                    502,
                    503,
                    504,
                ):
                    raise
                if attempt == 3:
                    raise
                time.sleep(0.5 * 2**attempt)

    def product(self):
        data = self._get(f"/products/{self.product_id}")
        if data.get("is_disabled") or data.get("trading_disabled"):
            raise ValueError(f"{self.product_id} trading is disabled")
        return Product(
            **{
                k: D(data[k])
                for k in ("base_increment", "quote_increment", "base_min_size", "quote_min_size")
            }
        )

    def quote(self):
        book = self._get("/product_book", {"product_id": self.product_id, "limit": 1})["pricebook"]
        return Quote(
            timestamp(book["time"]),
            D(book["bids"][0]["price"]),
            D(book["asks"][0]["price"]),
            D(book["bids"][0]["size"]),
            D(book["asks"][0]["size"]),
        )

    def candles(self, start, end, step=86400):
        granularity = {60: "ONE_MINUTE", 86400: "ONE_DAY"}[step]
        result = {}
        cursor = start
        while cursor < end:
            stop = min(end, cursor + 350 * step)
            data = self._get(
                f"/products/{self.product_id}/candles",
                # Coinbase includes the end bucket; leave it outside this page.
                {"start": cursor, "end": stop - 1, "granularity": granularity, "limit": 350},
            )
            batch = {}
            for row in data["candles"]:
                t = int(row["start"])
                if cursor <= t < stop:
                    if t in batch:
                        raise ValueError("Coinbase returned duplicate candle timestamps")
                    batch[t] = Candle(
                        t, *(D(row[k]) for k in ("open", "high", "low", "close", "volume"))
                    )
            candles = sorted(batch.values(), key=lambda c: c.start)
            try:
                validate_candles(candles, cursor, stop, step)
            except ValueError as exc:
                expected = set(range(cursor, stop, step))
                missing = sorted(expected - {c.start for c in candles})
                raise ValueError(
                    f"Incomplete candles for {self.product_id}: step={step}, "
                    f"window={cursor}..{stop}, missing={missing[:10]} "
                    f"({len(missing)} missing)"
                ) from exc
            result.update(batch)
            cursor = stop
            time.sleep(0.12)
        return sorted(result.values(), key=lambda c: c.start)


class QuoteStream:
    """Level 2 book with sequence checks; disconnects invalidate the cached quote."""

    def __init__(self):
        self.latest = None
        self.error = None

    async def run(self):
        backoff = 1
        while True:
            try:
                async with connect(
                    "wss://advanced-trade-ws.coinbase.com",
                    ssl=ssl.create_default_context(cafile=certifi.where()),
                    open_timeout=10,
                    ping_interval=20,
                    # BTC's full Level 2 snapshot can exceed 4 MiB.
                    max_size=32 * 1024 * 1024,
                ) as ws:
                    for channel in ("heartbeats", "level2"):
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "subscribe",
                                    "product_ids": ["BTC-USD"],
                                    "channel": channel,
                                }
                            )
                        )
                    bids, asks, sequence = {}, {}, None
                    backoff = 1
                    async for message in ws:
                        data = json.loads(message)
                        # Subscription acknowledgments and heartbeats also consume
                        # sequence numbers on this connection, not just book updates.
                        sequence = advance_sequence(sequence, data)
                        if data.get("channel") != "l2_data":
                            if data.get("type") == "error":
                                raise ValueError("Coinbase WebSocket subscription error")
                            continue
                        for event in data["events"]:
                            if event["product_id"] != "BTC-USD":
                                continue
                            if event["type"] == "snapshot":
                                bids.clear()
                                asks.clear()
                            for update in event["updates"]:
                                book = bids if update["side"] == "bid" else asks
                                price, size = D(update["price_level"]), D(update["new_quantity"])
                                if size == 0:
                                    book.pop(price, None)
                                else:
                                    book[price] = size
                        if bids and asks:
                            bid, ask = max(bids), min(asks)
                            self.latest = Quote(
                                timestamp(data["timestamp"]), bid, ask, bids[bid], asks[ask]
                            )
                            self.error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.latest = None
                # This connection is public-only and has no credentials to redact.
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)
