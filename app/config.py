from __future__ import annotations

import os
import re
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
    telegram_channel_ids: tuple[str, ...]
    signal_db_path: str
    signal_history_limit: int
    outcome_backend: str
    database_url: str | None
    database_schema: str
    database_pool_min: int
    database_pool_max: int
    database_ssl_require: bool
    lifecycle_monitor_seconds: float

    @property
    def telegram_delivery_ids(self) -> tuple[str, ...]:
        """All alert destinations, de-duplicated without broadening command access."""
        return tuple(dict.fromkeys(self.telegram_chat_ids + self.telegram_channel_ids))

    @property
    def resolved_outcome_backend(self) -> str:
        if self.outcome_backend == "auto":
            return "postgres" if self.database_url else "sqlite"
        return self.outcome_backend

    @classmethod
    def from_env(cls) -> Settings:
        candle_limit = min(250, max(220, int(os.getenv("CANDLE_LIMIT", "250"))))
        primary_chat_id = os.getenv("TELEGRAM_CHAT_ID") or None
        configured_chat_ids = _csv("TELEGRAM_CHAT_IDS", ())
        telegram_chat_ids = tuple(dict.fromkeys((primary_chat_id,) + configured_chat_ids)) if primary_chat_id else tuple(dict.fromkeys(configured_chat_ids))
        telegram_channel_ids = tuple(dict.fromkeys(_csv("TELEGRAM_CHANNEL_IDS", ())))
        database_pool_min = max(1, min(5, int(os.getenv("DATABASE_POOL_MIN", "1"))))
        database_pool_max = max(database_pool_min, min(10, int(os.getenv("DATABASE_POOL_MAX", "3"))))
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
            telegram_channel_ids=telegram_channel_ids,
            signal_db_path=os.getenv("SIGNAL_DB_PATH", "/tmp/prism_signals.db"),
            signal_history_limit=max(100, min(50_000, int(os.getenv("SIGNAL_HISTORY_LIMIT", "5000")))),
            outcome_backend=os.getenv("OUTCOME_BACKEND", "auto").strip().lower(),
            database_url=os.getenv("DATABASE_URL") or None,
            database_schema=os.getenv("DATABASE_SCHEMA", "prism").strip().lower(),
            database_pool_min=database_pool_min,
            database_pool_max=database_pool_max,
            database_ssl_require=_bool("DATABASE_SSL_REQUIRE", True),
            lifecycle_monitor_seconds=max(15.0, min(900.0, float(os.getenv("LIFECYCLE_MONITOR_SECONDS", "60")))),
        )

    def validate(self) -> None:
        if self.exchange not in {"binance", "bybit"}:
            raise ValueError("EXCHANGE must be 'binance' or 'bybit'")
        if not self.dry_run and (not self.telegram_bot_token or not self.telegram_delivery_ids):
            raise ValueError("Telegram token and at least one chat or channel destination are required unless DRY_RUN=true")
        if not self.watchlist:
            raise ValueError("WATCHLIST cannot be empty")
        if self.outcome_backend not in {"auto", "sqlite", "postgres"}:
            raise ValueError("OUTCOME_BACKEND must be 'auto', 'sqlite', or 'postgres'")
        if self.resolved_outcome_backend == "postgres" and not self.database_url:
            raise ValueError("DATABASE_URL is required when OUTCOME_BACKEND=postgres")
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", self.database_schema):
            raise ValueError("DATABASE_SCHEMA must be a lowercase PostgreSQL identifier")
