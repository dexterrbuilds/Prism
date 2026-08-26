from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from app.models import Signal, SignalState
from app.signals.lifecycle import ALLOWED_TRANSITIONS
from app.signals.outcomes import PerformanceStats, deserialize_signal, serialize_signal

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PostgresOutcomeRepository:
    """Supabase-compatible Postgres signal ledger with atomic lifecycle writes."""

    def __init__(self, pool: asyncpg.Pool, schema: str, history_limit: int) -> None:
        if not _IDENTIFIER.fullmatch(schema):
            raise ValueError("DATABASE_SCHEMA must be a lowercase PostgreSQL identifier")
        self._pool = pool
        self._schema = schema
        self._history_limit = history_limit

    @classmethod
    async def create(
        cls,
        database_url: str,
        *,
        schema: str = "prism",
        history_limit: int = 5000,
        pool_min: int = 1,
        pool_max: int = 3,
        ssl_required: bool = True,
    ) -> PostgresOutcomeRepository:
        if not _IDENTIFIER.fullmatch(schema):
            raise ValueError("DATABASE_SCHEMA must be a lowercase PostgreSQL identifier")
        pool = await asyncpg.create_pool(
            dsn=database_url,
            min_size=pool_min,
            max_size=pool_max,
            command_timeout=15,
            ssl="require" if ssl_required else None,
            # Supavisor transaction mode does not support prepared statements.
            # Disabling this is harmless for the preferred session-pooler URL.
            statement_cache_size=0,
        )
        assert pool is not None
        repository = cls(pool, schema, history_limit)
        try:
            await repository._initialize()
        except BaseException:
            await pool.close()
            raise
        return repository

    @property
    def backend(self) -> str:
        return "postgres"

    @property
    def location(self) -> str:
        return f"schema:{self._schema}"

    @property
    def _outcomes(self) -> str:
        return f'"{self._schema}".signal_outcomes'

    @property
    def _metadata(self) -> str:
        return f'"{self._schema}".metadata'

    async def _initialize(self) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._metadata} (
                    key TEXT PRIMARY KEY,
                    value TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS {self._outcomes} (
                    signal_id TEXT PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    state TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    current_price DOUBLE PRECISION,
                    activated_at TIMESTAMPTZ,
                    tp1_hit_at TIMESTAMPTZ,
                    tp2_hit_at TIMESTAMPTZ,
                    stopped_at TIMESTAMPTZ,
                    invalidated_at TIMESTAMPTZ,
                    win BOOLEAN NOT NULL DEFAULT FALSE,
                    payload JSONB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_prism_outcomes_created
                    ON {self._outcomes}(created_at);
                CREATE INDEX IF NOT EXISTS idx_prism_outcomes_state
                    ON {self._outcomes}(state);
                """
            )
            await connection.execute(
                f"INSERT INTO {self._metadata}(key, value) VALUES ('tracking_started_at', $1) ON CONFLICT (key) DO NOTHING",
                datetime.now(UTC),
            )

    async def contains_signal(self, signal_id: str) -> bool:
        async with self._pool.acquire() as connection:
            return bool(await connection.fetchval(f"SELECT EXISTS(SELECT 1 FROM {self._outcomes} WHERE signal_id = $1)", signal_id))

    async def _insert_signal(self, connection: asyncpg.Connection, signal: Signal) -> bool:
        now = signal.state_changed_at or signal.created_at
        activated_at = signal.activated_at or (now if signal.state is SignalState.ACTIVE else None)
        result: str = await connection.execute(
            f"""
            INSERT INTO {self._outcomes}(
                signal_id, created_at, updated_at, state, symbol, strategy,
                direction, score, current_price, activated_at, payload
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            ON CONFLICT (signal_id) DO NOTHING
            """,
            signal.id,
            signal.created_at,
            now,
            signal.state.value,
            signal.symbol,
            signal.strategy,
            signal.direction.value,
            signal.score,
            signal.current_price,
            activated_at,
            serialize_signal(signal),
        )
        return result == "INSERT 0 1"

    async def record_signal(self, signal: Signal) -> bool:
        async with self._pool.acquire() as connection, connection.transaction():
            inserted = await self._insert_signal(connection, signal)
            if inserted:
                await self._prune(connection)
            return inserted

    async def record_event(self, signal: Signal) -> bool:
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"SELECT state FROM {self._outcomes} WHERE signal_id = $1 FOR UPDATE",
                signal.id,
            )
            if row is None:
                if not await self._insert_signal(connection, signal):
                    return False
            else:
                persisted_state = SignalState(str(row["state"]))
                if signal.state is persisted_state or signal.state not in ALLOWED_TRANSITIONS[persisted_state]:
                    return False

            event_at = signal.state_changed_at or datetime.now(UTC)
            timestamp_fields: dict[SignalState, tuple[str, ...]] = {
                SignalState.ACTIVE: ("activated_at",),
                SignalState.TP1_HIT: ("tp1_hit_at",),
                SignalState.TP2_HIT: ("tp1_hit_at", "tp2_hit_at"),
                SignalState.STOPPED: ("stopped_at",),
                SignalState.INVALIDATED: ("invalidated_at",),
            }
            assignments = ["updated_at = $2", "state = $3", "current_price = $4", "payload = $5::jsonb"]
            values: list[Any] = [signal.id, event_at, signal.state.value, signal.current_price, serialize_signal(signal)]
            for field in timestamp_fields.get(signal.state, ()):
                values.append(event_at)
                assignments.append(f"{field} = COALESCE({field}, ${len(values)})")
            if signal.state in {SignalState.TP1_HIT, SignalState.TP2_HIT}:
                assignments.append("win = TRUE")
            await connection.execute(
                f"UPDATE {self._outcomes} SET {', '.join(assignments)} WHERE signal_id = $1",
                *values,
            )
            return True

    async def load_open_signals(self) -> tuple[Signal, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"SELECT payload::text AS payload FROM {self._outcomes} WHERE state = ANY($1::text[])",
                [SignalState.CONFIRMED.value, SignalState.ACTIVE.value, SignalState.TP1_HIT.value],
            )
        signals: list[Signal] = []
        for row in rows:
            try:
                signals.append(deserialize_signal(str(row["payload"])))
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(signals)

    async def stats(self, period_days: int | None = None) -> PerformanceStats:
        current = datetime.now(UTC)
        async with self._pool.acquire() as connection:
            tracking_since = await connection.fetchval(
                f"SELECT value FROM {self._metadata} WHERE key = 'tracking_started_at'"
            )
            if not isinstance(tracking_since, datetime):
                tracking_since = current
            cutoff = max(tracking_since, current - timedelta(days=period_days)) if period_days is not None else tracking_since
            rows = await connection.fetch(
                f"""
                SELECT state, win, activated_at, tp1_hit_at, tp2_hit_at,
                       stopped_at, invalidated_at
                FROM {self._outcomes} WHERE created_at >= $1
                """,
                cutoff,
            )
        wins = sum(bool(row["win"]) for row in rows)
        losses = sum(row["state"] == SignalState.STOPPED.value and not bool(row["win"]) for row in rows)
        durations: list[float] = []
        for row in rows:
            activated = row["activated_at"]
            resolved = row["tp1_hit_at"] if bool(row["win"]) else row["stopped_at"]
            if isinstance(activated, datetime) and isinstance(resolved, datetime) and resolved >= activated:
                durations.append((resolved - activated).total_seconds() / 3600)
        return PerformanceStats(
            tracking_since=cutoff,
            period_days=period_days,
            signals=len(rows),
            activated=sum(row["activated_at"] is not None for row in rows),
            wins=wins,
            losses=losses,
            open_signals=sum(row["state"] in {SignalState.CONFIRMED.value, SignalState.ACTIVE.value} for row in rows),
            tp1_runners=sum(row["state"] == SignalState.TP1_HIT.value for row in rows),
            tp2_hits=sum(row["tp2_hit_at"] is not None for row in rows),
            invalidated=sum(row["invalidated_at"] is not None for row in rows),
            average_hold_hours=sum(durations) / len(durations) if durations else None,
        )

    async def _prune(self, connection: asyncpg.Connection) -> None:
        count = int(await connection.fetchval(f"SELECT COUNT(*) FROM {self._outcomes}"))
        excess = max(0, count - self._history_limit)
        if not excess:
            return
        await connection.execute(
            f"""
            DELETE FROM {self._outcomes} WHERE signal_id IN (
                SELECT signal_id FROM {self._outcomes}
                WHERE state = ANY($1::text[]) ORDER BY created_at ASC LIMIT $2
            )
            """,
            [
                SignalState.TP2_HIT.value,
                SignalState.STOPPED.value,
                SignalState.INVALIDATED.value,
                SignalState.EXPIRED.value,
            ],
            excess,
        )

    async def close(self) -> None:
        await self._pool.close()
