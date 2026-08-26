from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.models import Direction


class MascotState(StrEnum):
    HUGE_WIN = "huge-win"
    BIG_WIN = "big-win"
    WIN = "win"
    CONFIDENT = "confident"
    LOSS = "loss"
    SMALL_LOSS = "small-loss"
    NEUTRAL = "neutral"
    STREAK = "streak"


@dataclass(frozen=True, slots=True)
class MascotThresholds:
    huge_win_percent: float = 50.0
    big_win_percent: float = 20.0
    large_loss_percent: float = -15.0

    def __post_init__(self) -> None:
        if self.huge_win_percent <= self.big_win_percent:
            raise ValueError("huge_win_percent must exceed big_win_percent")
        if self.big_win_percent <= 0:
            raise ValueError("big_win_percent must be positive")
        if self.large_loss_percent >= 0:
            raise ValueError("large_loss_percent must be negative")


@dataclass(frozen=True, slots=True)
class PnlCardData:
    pair: str
    direction: Direction
    pnl_usd: float
    pnl_percent: float
    entry_price: float | None = None
    exit_price: float | None = None
    mark_price: float | None = None
    leverage: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    trade_duration: str | None = None
    calculation_label: str | None = None
    username: str = "prismquantbot"
    quote: str | None = None
    context_message: str | None = None
    mascot_state: MascotState | None = None
    chart_data: tuple[float, ...] | None = None
    content_seed: str | None = None

    def __post_init__(self) -> None:
        pair = self.pair.strip().upper()
        username = self.username.strip().lstrip("@") or "prismquantbot"
        if not pair:
            raise ValueError("pair cannot be empty")
        if self.chart_data is not None and len(self.chart_data) < 2:
            raise ValueError("chart_data must contain at least two observations")
        for name, value in (
            ("pnl_usd", self.pnl_usd),
            ("pnl_percent", self.pnl_percent),
            ("entry_price", self.entry_price),
            ("exit_price", self.exit_price),
            ("mark_price", self.mark_price),
            ("leverage", self.leverage),
        ):
            if value is not None and not (-1e100 < value < 1e100):
                raise ValueError(f"{name} must be finite")
        if self.leverage is not None and self.leverage <= 0:
            raise ValueError("leverage must be positive")
        object.__setattr__(self, "pair", pair)
        object.__setattr__(self, "username", username)
