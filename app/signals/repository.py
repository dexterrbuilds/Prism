from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from app.models import Signal
from app.signals.outcomes import OutcomeLedger, PerformanceStats

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)


class OutcomeRepository(Protocol):
    """Async persistence boundary shared by SQLite and Supabase Postgres."""

    @property
    def backend(self) -> str: ...

    @property
    def location(self) -> str: ...

    async def contains_signal(self, signal_id: str) -> bool: ...

    async def record_signal(self, signal: Signal) -> bool: ...

    async def record_event(self, signal: Signal) -> bool: ...

    async def load_open_signals(self) -> Sequence[Signal]: ...

    async def stats(self, period_days: int | None = None) -> PerformanceStats: ...

    async def close(self) -> None: ...


class SQLiteOutcomeRepository:
    """Non-blocking adapter around the compact local SQLite ledger."""

    def __init__(self, path: str, history_limit: int = 5000) -> None:
        self._ledger = OutcomeLedger(path, history_limit)
        self._lock = asyncio.Lock()

    @property
    def backend(self) -> str:
        return "sqlite"

    @property
    def location(self) -> str:
        return self._ledger.path

    async def _call(self, method: str, *args: object) -> object:
        async with self._lock:
            function = getattr(self._ledger, method)
            return await asyncio.to_thread(function, *args)

    async def contains_signal(self, signal_id: str) -> bool:
        return bool(await self._call("contains_signal", signal_id))

    async def record_signal(self, signal: Signal) -> bool:
        return bool(await self._call("record_signal", signal))

    async def record_event(self, signal: Signal) -> bool:
        return bool(await self._call("record_event", signal))

    async def load_open_signals(self) -> Sequence[Signal]:
        result = await self._call("load_open_signals")
        assert isinstance(result, tuple)
        return result

    async def stats(self, period_days: int | None = None) -> PerformanceStats:
        result = await self._call("stats", period_days)
        assert isinstance(result, PerformanceStats)
        return result

    async def close(self) -> None:
        await self._call("close")


async def create_outcome_repository(settings: Settings) -> OutcomeRepository:
    """Create the configured durable ledger without ever logging its DSN."""
    if settings.resolved_outcome_backend == "sqlite":
        return SQLiteOutcomeRepository(settings.signal_db_path, settings.signal_history_limit)

    from app.signals.postgres_outcomes import PostgresOutcomeRepository

    assert settings.database_url is not None
    last_error: BaseException | None = None
    for attempt in range(1, 4):
        try:
            return await PostgresOutcomeRepository.create(
                settings.database_url,
                schema=settings.database_schema,
                history_limit=settings.signal_history_limit,
                pool_min=settings.database_pool_min,
                pool_max=settings.database_pool_max,
                ssl_required=settings.database_ssl_require,
            )
        except Exception as exc:
            last_error = exc
            logger.warning("outcome_database_retry backend=postgres attempt=%d error=%s", attempt, type(exc).__name__)
            if attempt < 3:
                await asyncio.sleep(2 ** (attempt - 1))
    assert last_error is not None
    raise RuntimeError("Unable to connect to the outcome database after 3 attempts") from last_error
