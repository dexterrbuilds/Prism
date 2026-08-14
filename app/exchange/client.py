from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import ccxt.async_support as ccxt  # type: ignore[import-untyped]

from app.analysis.data_quality import from_ccxt_rows
from app.config import Settings
from app.models import CandleSeries

logger = logging.getLogger(__name__)


class ExchangeRequestError(RuntimeError):
    pass


@dataclass(slots=True)
class _CacheItem:
    created_monotonic: float
    value: CandleSeries


class ExchangeClient:
    """One persistent, rate-limited futures exchange client."""

    def __init__(self, settings: Settings) -> None:
        exchange_class = getattr(ccxt, settings.exchange)
        options: dict[str, Any] = {"defaultType": "future" if settings.exchange == "binance" else "swap"}
        self._client = exchange_class(
            {
                "enableRateLimit": True,
                "timeout": settings.request_timeout_ms,
                "options": options,
            }
        )
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.request_concurrency)
        self._cache: dict[tuple[str, str], _CacheItem] = {}

    @property
    def name(self) -> str:
        return self._settings.exchange

    async def load_markets(self) -> None:
        await self._request("load_markets", self._client.load_markets)

    async def fetch_ohlcv(self, symbol: str, timeframe: str, as_of_ms: int) -> CandleSeries:
        key = (symbol, timeframe)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached.created_monotonic < 20:
            return cached.value

        async def operation() -> list[list[float]]:
            rows = await self._client.fetch_ohlcv(
                self._futures_symbol(symbol),
                timeframe=timeframe,
                limit=self._settings.candle_limit,
            )
            if not isinstance(rows, list):
                raise ExchangeRequestError("malformed OHLCV response")
            return rows

        rows = await self._request(f"fetch_ohlcv:{symbol}:{timeframe}", operation)
        value = from_ccxt_rows(symbol, timeframe, rows, as_of_ms)
        self._cache[key] = _CacheItem(time.monotonic(), value)
        return value

    @staticmethod
    def _futures_symbol(symbol: str) -> str:
        """Map the configured spot-style label to CCXT's linear contract symbol."""
        if ":" in symbol:
            return symbol
        base, separator, quote = symbol.partition("/")
        if not separator or not base or not quote:
            raise ExchangeRequestError(f"invalid symbol: {symbol}")
        return f"{base}/{quote}:{quote}"

    async def _request(self, name: str, operation: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._settings.request_retries + 1):
            try:
                async with self._semaphore:
                    return await operation()
            except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RateLimitExceeded) as exc:
                last_error = exc
                if attempt >= self._settings.request_retries:
                    break
                delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.2)
                logger.warning("exchange_retry name=%s attempt=%d delay=%.2f error=%s", name, attempt + 1, delay, type(exc).__name__)
                await asyncio.sleep(delay)
            except (ccxt.BaseError, ValueError, TypeError) as exc:
                raise ExchangeRequestError(f"{name}: {type(exc).__name__}") from exc
        raise ExchangeRequestError(f"{name}: retries exhausted ({type(last_error).__name__})") from last_error

    def clear_cycle_cache(self) -> None:
        self._cache.clear()

    async def close(self) -> None:
        self._cache.clear()
        await self._client.close()
