from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_WATCHLIST = (
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or default


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    exchange: str
    watchlist: tuple[str, ...]
    timeframes: tuple[str, ...]
    candle_limit: int
    scan_interval_seconds: float
    request_timeout_ms: int
    request_concurrency: int
    request_retries: int
    port: int
    dry_run: bool
    send_watch_alerts: bool
    minimum_valid_score: int
    pivot_left: int
    pivot_right: int
    zone_atr_tolerance: float
    max_chase_atr: float
    log_level: str
    telegram_chat_ids: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Settings:
        candle_limit = min(250, max(220, int(os.getenv("CANDLE_LIMIT", "250"))))
        primary_chat_id = os.getenv("TELEGRAM_CHAT_ID") or None
        configured_chat_ids = _csv("TELEGRAM_CHAT_IDS", ())
        telegram_chat_ids = tuple(dict.fromkeys((primary_chat_id,) + configured_chat_ids)) if primary_chat_id else tuple(dict.fromkeys(configured_chat_ids))
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=primary_chat_id,
            exchange=os.getenv("EXCHANGE", "binance").lower(),
            watchlist=_csv("WATCHLIST", DEFAULT_WATCHLIST),
            timeframes=("4h", "1h", "15m"),
            candle_limit=candle_limit,
            scan_interval_seconds=max(15.0, float(os.getenv("SCAN_INTERVAL_SECONDS", "2700"))),
            request_timeout_ms=max(1_000, int(os.getenv("REQUEST_TIMEOUT_MS", "15000"))),
            request_concurrency=max(1, min(10, int(os.getenv("REQUEST_CONCURRENCY", "3")))),
            request_retries=max(0, min(6, int(os.getenv("REQUEST_RETRIES", "3")))),
            port=int(os.getenv("PORT", "10000")),
            dry_run=_bool("DRY_RUN", True),
            send_watch_alerts=_bool("SEND_WATCH_ALERTS", False),
            minimum_valid_score=max(70, min(100, int(os.getenv("MINIMUM_VALID_SCORE", "80")))),
            pivot_left=max(2, int(os.getenv("PIVOT_LEFT", "3"))),
            pivot_right=max(2, int(os.getenv("PIVOT_RIGHT", "3"))),
            zone_atr_tolerance=max(0.05, float(os.getenv("ZONE_ATR_TOLERANCE", "0.5"))),
            max_chase_atr=max(0.2, float(os.getenv("MAX_CHASE_ATR", "0.75"))),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            telegram_chat_ids=telegram_chat_ids,
        )

    def validate(self) -> None:
        if self.exchange not in {"binance", "bybit"}:
            raise ValueError("EXCHANGE must be 'binance' or 'bybit'")
        if not self.dry_run and (not self.telegram_bot_token or not self.telegram_chat_ids):
            raise ValueError("Telegram token and at least one chat ID are required unless DRY_RUN=true")
        if not self.watchlist:
            raise ValueError("WATCHLIST cannot be empty")
