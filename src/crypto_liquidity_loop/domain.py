from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

D = Decimal
DAY = 86400


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class Candle:
    start: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self):
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(v.is_finite() for v in values):
            raise ValueError("Nonfinite candle")
        if (
            self.low <= 0
            or self.volume < 0
            or not (
                self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high
            )
        ):
            raise ValueError("Invalid OHLC/volume")


@dataclass(frozen=True)
class Quote:
    timestamp: float
    bid: Decimal
    ask: Decimal
    bid_size: Decimal = D("0")
    ask_size: Decimal = D("0")

    def fresh(self, now, max_age):
        return (
            all(v.is_finite() for v in (self.bid, self.ask, self.bid_size, self.ask_size))
            and 0 < self.bid <= self.ask
            and self.bid_size >= 0
            and self.ask_size >= 0
            and -2 <= now - self.timestamp <= max_age
        )


@dataclass(frozen=True)
class Product:
    base_increment: Decimal = D("0.00000001")
    quote_increment: Decimal = D("0.01")
    base_min_size: Decimal = D("0.00001")
    quote_min_size: Decimal = D("1")

    def __post_init__(self):
        for v in (
            self.base_increment,
            self.quote_increment,
            self.base_min_size,
            self.quote_min_size,
        ):
            if not v.is_finite() or v <= 0:
                raise ValueError("Invalid product increments or minimum sizes")

    def valid(self, quantity, price):
        return quantity >= self.base_min_size and quantity * price >= self.quote_min_size

    def buy_quantity(self, cash, price, fee):
        return floor_step(cash / (price * (1 + fee)), self.base_increment)


def validate_candles(candles, start, end, step=DAY):
    if start % step or end % step or end <= start:
        raise ValueError("Candle range must use aligned, increasing UTC boundaries")
    actual = [c.start for c in candles]
    if actual != list(range(start, end, step)):
        raise ValueError("Incomplete candles: gaps, duplicates, order, or unexpected timestamps")


def signal(candles, config):
    needed = max(config.sma_days, config.entry_days + 1, config.exit_days + 1)
    if len(candles) < needed:
        return {"action": "warmup", "timestamp": None}
    window = candles[-needed:]
    validate_candles(window, window[0].start, window[-1].start + DAY)
    close = candles[-1].close
    high = max(c.high for c in candles[-config.entry_days - 1 : -1])
    low = min(c.low for c in candles[-config.exit_days - 1 : -1])
    sma = sum((c.close for c in candles[-config.sma_days :]), D(0)) / config.sma_days
    action = "enter" if close > high and close > sma else "exit" if close < low else "hold"
    return {
        "action": action,
        "timestamp": candles[-1].start + DAY,
        "close": str(close),
        "channel_high": str(high),
        "channel_low": str(low),
        "sma": str(sma),
    }


def equity(state, bid):
    return D(state["cash"]) + D(state["btc"]) * bid


def drawdown(value, peak):
    return max(D(0), (peak - value) / peak) if peak > 0 else D(0)
