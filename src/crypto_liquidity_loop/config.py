import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Config:
    mode: str = "paper"
    database: str = "var/paper.sqlite3"
    initial_cash: Decimal = Decimal("500")
    entry_days: int = 55
    exit_days: int = 20
    sma_days: int = 200
    maker_fee: Decimal = Decimal("0.005")
    taker_fee: Decimal = Decimal("0.009")
    drawdown_limit: Decimal = Decimal("0.70")
    poll_seconds: int = 5
    quote_max_age_seconds: int = 15
    entry_window_seconds: int = 3600
    exit_window_seconds: int = 60
    monthly_operating_cost: Decimal = Decimal("5")
    volume_participation: Decimal = Decimal("0.01")
    slippage_bps: Decimal = Decimal("10")
    spread_bps: Decimal = Decimal("2")

    def __post_init__(self):
        if self.mode != "paper":
            raise ValueError("Only paper mode is supported. Live orders are not implemented.")
        for key in (
            "entry_days",
            "exit_days",
            "sma_days",
            "poll_seconds",
            "quote_max_age_seconds",
            "entry_window_seconds",
            "exit_window_seconds",
        ):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if self.poll_seconds > 5:
            raise ValueError("Risk monitoring must run at least every five seconds")
        for key in ("maker_fee", "taker_fee", "drawdown_limit", "volume_participation"):
            value = getattr(self, key)
            if not value.is_finite() or not Decimal(0) <= value < Decimal(1):
                raise ValueError(f"{key} must be finite and in [0, 1)")
        if not 0 < self.drawdown_limit < 1 or not 0 < self.volume_participation < 1:
            raise ValueError("Drawdown limit and volume participation must be positive")
        for key in ("initial_cash", "monthly_operating_cost", "slippage_bps", "spread_bps"):
            value = getattr(self, key)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{key} must be finite and nonnegative")
        if self.initial_cash <= 0 or self.slippage_bps >= 10000 or self.spread_bps >= 10000:
            raise ValueError("Invalid initial cash or execution cost assumption")

    @classmethod
    def load(cls, path="config.toml"):
        data = tomllib.loads(Path(path).read_text()) if Path(path).exists() else {}
        defaults = cls()
        for key in data:
            if not hasattr(defaults, key) or key not in asdict(defaults):
                raise ValueError(f"Unknown setting: {key}")
            if isinstance(getattr(defaults, key), Decimal):
                data[key] = Decimal(str(data[key]))
        return cls(**data)

    def public(self):
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}

    def fingerprint(self):
        data = self.public()
        data.pop("database")
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
