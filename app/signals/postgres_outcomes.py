from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from app.models import PublicationState, Signal, SignalState
from app.signals.lifecycle import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    OPEN_STATES,
    STOP_STATES,
)
from app.signals.outcomes import PerformanceStats, deserialize_signal, serialize_signal

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def serialize_event_metadata(signal: Signal) -> str:
    return json.dumps(
        {
            "state": signal.state.value,
            "reason": signal.lifecycle_reason,
            "setup_fingerprint": signal.setup_fingerprint,
            "publication_state": signal.publication_state.value,
        },
        separators=(",", ":"),
    )


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

    @property
    def _events(self) -> str:
        return f'"{self._schema}".signal_events'

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
                    expires_at TIMESTAMPTZ,
                    entry_trigger_price DOUBLE PRECISION,
                    missed_at TIMESTAMPTZ,
                    expired_at TIMESTAMPTZ,
                    lifecycle_reason TEXT,
                    mode TEXT NOT NULL DEFAULT 'INTRADAY',
                    entry_quality_score INTEGER,
                    atr_at_entry DOUBLE PRECISION,
                    mae DOUBLE PRECISION NOT NULL DEFAULT 0,
                    mfe DOUBLE PRECISION NOT NULL DEFAULT 0,
                    stopped_then_target_reached BOOLEAN NOT NULL DEFAULT FALSE,
                    follow_up_until TIMESTAMPTZ,
                    setup_fingerprint TEXT,
                    signal_type TEXT NOT NULL DEFAULT 'INITIAL',
                    parent_signal_id TEXT,
                    setup_origin_at TIMESTAMPTZ,
                    major_structure_level DOUBLE PRECISION,
                    last_evaluated_at TIMESTAMPTZ,
                    terminal_state TEXT,
                    terminal_at TIMESTAMPTZ,
                    result TEXT,
                    publication_state TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN',
                    published_at TIMESTAMPTZ,
                    channel_published_at TIMESTAMPTZ,
                    channel_message_id TEXT,
                    dm_delivery_attempted_at TIMESTAMPTZ,
                    dm_success_count INTEGER NOT NULL DEFAULT 0,
                    dm_failure_count INTEGER NOT NULL DEFAULT 0,
                    publish_attempts INTEGER NOT NULL DEFAULT 0,
                    last_publish_error TEXT,
                    win BOOLEAN NOT NULL DEFAULT FALSE,
                    payload JSONB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS {self._events} (
                    event_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_at TIMESTAMPTZ NOT NULL,
                    price DOUBLE PRECISION,
                    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    UNIQUE(signal_id, event_type)
                );
                CREATE INDEX IF NOT EXISTS idx_prism_outcomes_created
                    ON {self._outcomes}(created_at);
                CREATE INDEX IF NOT EXISTS idx_prism_outcomes_state
                    ON {self._outcomes}(state);
                """
            )
            # Create one immutable server-side safety copy before the additive
            # identity migration.  Existing backups are never overwritten.
            await connection.execute(
                f"""
                DO $migration$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = '{self._schema}'
                          AND table_name = 'signal_outcomes'
                          AND column_name = 'setup_fingerprint'
                    ) THEN
                        EXECUTE 'CREATE TABLE IF NOT EXISTS "{self._schema}".signal_outcomes_pre_identity_v3_backup AS TABLE {self._outcomes}';
                    END IF;
                END
                $migration$;
                """
            )
            await connection.execute(
                f"""
                DO $migration$
                BEGIN
                    IF (
                        SELECT COUNT(*) FROM information_schema.columns
                        WHERE table_schema = '{self._schema}'
                          AND table_name = 'signal_outcomes'
                          AND column_name IN (
                              'publication_state', 'published_at', 'channel_published_at',
                              'channel_message_id', 'dm_delivery_attempted_at',
                              'dm_success_count', 'dm_failure_count', 'publish_attempts',
                              'last_publish_error'
                          )
                    ) < 9 THEN
                        EXECUTE 'CREATE TABLE IF NOT EXISTS "{self._schema}".signal_outcomes_pre_publication_v4_backup AS TABLE {self._outcomes}';
                    END IF;
                END
                $migration$;
                """
            )
            await connection.execute(
                f"""
                ALTER TABLE {self._outcomes}
                    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS entry_trigger_price DOUBLE PRECISION,
                    ADD COLUMN IF NOT EXISTS missed_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS lifecycle_reason TEXT,
                    ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'INTRADAY',
                    ADD COLUMN IF NOT EXISTS entry_quality_score INTEGER,
                    ADD COLUMN IF NOT EXISTS atr_at_entry DOUBLE PRECISION,
                    ADD COLUMN IF NOT EXISTS mae DOUBLE PRECISION NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS mfe DOUBLE PRECISION NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS stopped_then_target_reached BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS follow_up_until TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS setup_fingerprint TEXT,
                    ADD COLUMN IF NOT EXISTS signal_type TEXT NOT NULL DEFAULT 'INITIAL',
                    ADD COLUMN IF NOT EXISTS parent_signal_id TEXT,
                    ADD COLUMN IF NOT EXISTS setup_origin_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS major_structure_level DOUBLE PRECISION,
                    ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS terminal_state TEXT,
                    ADD COLUMN IF NOT EXISTS terminal_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS result TEXT,
                    ADD COLUMN IF NOT EXISTS publication_state TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN',
                    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS channel_published_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS channel_message_id TEXT,
                    ADD COLUMN IF NOT EXISTS dm_delivery_attempted_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS dm_success_count INTEGER NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS dm_failure_count INTEGER NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS publish_attempts INTEGER NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS last_publish_error TEXT
                """
            )
            await connection.execute(f"CREATE INDEX IF NOT EXISTS idx_prism_outcomes_mode ON {self._outcomes}(mode)")
            await connection.execute(f"CREATE INDEX IF NOT EXISTS idx_prism_outcomes_strategy ON {self._outcomes}(strategy)")
            await connection.execute(f"CREATE INDEX IF NOT EXISTS idx_prism_outcomes_setup ON {self._outcomes}(setup_fingerprint)")
            await connection.execute(f"CREATE INDEX IF NOT EXISTS idx_prism_outcomes_symbol_state ON {self._outcomes}(symbol, state)")
            await connection.execute(f"CREATE INDEX IF NOT EXISTS idx_prism_outcomes_publication ON {self._outcomes}(publication_state, state)")
            await connection.execute(f"CREATE INDEX IF NOT EXISTS idx_prism_events_signal ON {self._events}(signal_id, event_at)")
            await connection.execute(
                f"""
                INSERT INTO {self._events}(event_id, signal_id, event_type, event_at, price, metadata)
                SELECT signal_id || ':MIGRATED_SNAPSHOT', signal_id, 'MIGRATED_SNAPSHOT',
                       updated_at, current_price, jsonb_build_object('state', state)
                FROM {self._outcomes}
                ON CONFLICT DO NOTHING
                """
            )
            await connection.execute(
                f"""
                INSERT INTO {self._metadata}(key, value)
                SELECT 'tracking_started_at', COALESCE(MIN(created_at), $1)
                FROM {self._outcomes}
                ON CONFLICT (key) DO NOTHING
                """,
                datetime.now(UTC),
            )

    async def contains_signal(self, signal_id: str) -> bool:
        async with self._pool.acquire() as connection:
            return bool(await connection.fetchval(f"SELECT EXISTS(SELECT 1 FROM {self._outcomes} WHERE signal_id = $1)", signal_id))

    async def _insert_signal(self, connection: asyncpg.Connection, signal: Signal) -> bool:
        now = signal.state_changed_at or signal.created_at
        activated_at = signal.activated_at or (now if signal.state in ACTIVE_STATES else None)
        result: str = await connection.execute(
            f"""
            INSERT INTO {self._outcomes}(
                signal_id, created_at, updated_at, state, symbol, strategy,
                direction, score, current_price, activated_at, expires_at,
                entry_trigger_price, mode, entry_quality_score, atr_at_entry,
                mae, mfe, stopped_then_target_reached, follow_up_until,
                setup_fingerprint, signal_type, parent_signal_id, setup_origin_at,
                major_structure_level, last_evaluated_at, terminal_state,
                terminal_at, result, publication_state, published_at,
                channel_published_at, channel_message_id, dm_delivery_attempted_at,
                dm_success_count, dm_failure_count, publish_attempts,
                last_publish_error, payload
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                      $13, $14, $15, $16, $17, $18, $19, $20, $21, $22,
                      $23, $24, $25, $26, $27, $28, $29, $30, $31, $32,
                      $33, $34, $35, $36, $37, $38::jsonb)
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
            signal.expires_at,
            signal.entry_trigger_price,
            signal.mode.value,
            signal.entry_quality.total if signal.entry_quality else None,
            signal.atr_at_entry,
            signal.mae,
            signal.mfe,
            signal.stopped_then_target_reached,
            signal.follow_up_until,
            signal.setup_fingerprint,
            signal.signal_type,
            signal.parent_signal_id,
            signal.setup_origin_at,
            signal.major_structure_level,
            signal.last_evaluated_at,
            signal.terminal_state,
            signal.terminal_at,
            signal.result,
            signal.publication_state.value,
            signal.published_at,
            signal.channel_published_at,
            signal.channel_message_id,
            signal.dm_delivery_attempted_at,
            signal.dm_success_count,
            signal.dm_failure_count,
            signal.publish_attempts,
            signal.last_publish_error,
            serialize_signal(signal),
        )
        inserted = result == "INSERT 0 1"
        if inserted:
            await self._insert_event(connection, signal, "CREATED", signal.created_at)
        return inserted

    async def _insert_event(
        self,
        connection: asyncpg.Connection,
        signal: Signal,
        event_type: str,
        event_at: datetime,
    ) -> bool:
        result: str = await connection.execute(
            f"""
            INSERT INTO {self._events}(
                event_id, signal_id, event_type, event_at, price, metadata
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (signal_id, event_type) DO NOTHING
            """,
            f"{signal.id}:{event_type}",
            signal.id,
            event_type,
            event_at,
            signal.current_price,
            serialize_event_metadata(signal),
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
                SignalState.ENTRY_TRIGGERED: ("activated_at",),
                SignalState.TP1_HIT: ("tp1_hit_at",),
                SignalState.TP2_HIT: ("tp1_hit_at", "tp2_hit_at"),
                SignalState.STOPPED: ("stopped_at",),
                SignalState.SL_HIT: ("stopped_at",),
                SignalState.MISSED: ("missed_at",),
                SignalState.INVALIDATED: ("invalidated_at",),
                SignalState.EXPIRED: ("expired_at",),
            }
            assignments = [
                "updated_at = $2",
                "state = $3",
                "current_price = $4",
                "entry_trigger_price = COALESCE(entry_trigger_price, $5)",
                "lifecycle_reason = $6",
                "payload = $7::jsonb",
                "mode = $8",
                "entry_quality_score = $9",
                "atr_at_entry = $10",
                "mae = $11",
                "mfe = $12",
                "stopped_then_target_reached = $13",
                "follow_up_until = $14",
                "setup_fingerprint = $15",
                "signal_type = $16",
                "parent_signal_id = $17",
                "setup_origin_at = $18",
                "major_structure_level = $19",
                "last_evaluated_at = $20",
                "terminal_state = $21",
                "terminal_at = $22",
                "result = $23",
                "publication_state = $24",
                "published_at = $25",
                "channel_published_at = $26",
                "channel_message_id = $27",
                "dm_delivery_attempted_at = $28",
                "dm_success_count = $29",
                "dm_failure_count = $30",
                "publish_attempts = $31",
                "last_publish_error = $32",
            ]
            values: list[Any] = [
                signal.id,
                event_at,
                signal.state.value,
                signal.current_price,
                signal.entry_trigger_price,
                signal.lifecycle_reason,
                serialize_signal(signal),
                signal.mode.value,
                signal.entry_quality.total if signal.entry_quality else None,
                signal.atr_at_entry,
                signal.mae,
                signal.mfe,
                signal.stopped_then_target_reached,
                signal.follow_up_until,
                signal.setup_fingerprint,
                signal.signal_type,
                signal.parent_signal_id,
                signal.setup_origin_at,
                signal.major_structure_level,
                signal.last_evaluated_at,
                signal.terminal_state,
                signal.terminal_at,
                signal.result,
                signal.publication_state.value,
                signal.published_at,
                signal.channel_published_at,
                signal.channel_message_id,
                signal.dm_delivery_attempted_at,
                signal.dm_success_count,
                signal.dm_failure_count,
                signal.publish_attempts,
                signal.last_publish_error,
            ]
            for field in timestamp_fields.get(signal.state, ()):
                values.append(event_at)
                assignments.append(f"{field} = COALESCE({field}, ${len(values)})")
            if signal.state in {SignalState.TP1_HIT, SignalState.TP2_HIT}:
                assignments.append("win = TRUE")
            if not await self._insert_event(connection, signal, signal.state.value, event_at):
                return False
            await connection.execute(
                f"UPDATE {self._outcomes} SET {', '.join(assignments)} WHERE signal_id = $1",
                *values,
            )
            return True

    async def record_observation(self, signal: Signal) -> bool:
        async with self._pool.acquire() as connection:
            result: str = await connection.execute(
                f"""
                UPDATE {self._outcomes}
                SET updated_at = $2, current_price = $3, mae = $4, mfe = $5,
                    stopped_then_target_reached = $6, follow_up_until = $7,
                    last_evaluated_at = $8, terminal_state = $9,
                    terminal_at = $10, result = $11, publication_state = $12,
                    published_at = $13, channel_published_at = $14,
                    channel_message_id = $15, dm_delivery_attempted_at = $16,
                    dm_success_count = $17, dm_failure_count = $18,
                    publish_attempts = $19, last_publish_error = $20,
                    payload = $21::jsonb
                WHERE signal_id = $1
                """,
                signal.id,
                datetime.now(UTC),
                signal.current_price,
                signal.mae,
                signal.mfe,
                signal.stopped_then_target_reached,
                signal.follow_up_until,
                signal.last_evaluated_at,
                signal.terminal_state,
                signal.terminal_at,
                signal.result,
                signal.publication_state.value,
                signal.published_at,
                signal.channel_published_at,
                signal.channel_message_id,
                signal.dm_delivery_attempted_at,
                signal.dm_success_count,
                signal.dm_failure_count,
                signal.publish_attempts,
                signal.last_publish_error,
                serialize_signal(signal),
            )
        return result == "UPDATE 1"

    async def record_publication(self, signal: Signal) -> bool:
        """Persist Telegram publication metadata without changing lifecycle state."""
        event_at = signal.published_at or signal.dm_delivery_attempted_at or datetime.now(UTC)
        event_type = (
            "INITIAL_PUBLISHED"
            if signal.publication_state is PublicationState.PUBLISHED
            else "UNPUBLISHED_TERMINAL"
            if signal.publication_state is PublicationState.UNPUBLISHED_TERMINAL
            else "PUBLISH_FAILED"
        )
        async with self._pool.acquire() as connection, connection.transaction():
            result: str = await connection.execute(
                f"""
                UPDATE {self._outcomes}
                SET updated_at = $2, publication_state = $3, published_at = $4,
                    channel_published_at = $5, channel_message_id = $6,
                    dm_delivery_attempted_at = $7, dm_success_count = $8,
                    dm_failure_count = $9, publish_attempts = $10,
                    last_publish_error = $11, payload = $12::jsonb
                WHERE signal_id = $1
                """,
                signal.id,
                datetime.now(UTC),
                signal.publication_state.value,
                signal.published_at,
                signal.channel_published_at,
                signal.channel_message_id,
                signal.dm_delivery_attempted_at,
                signal.dm_success_count,
                signal.dm_failure_count,
                signal.publish_attempts,
                signal.last_publish_error,
                serialize_signal(signal),
            )
            if result != "UPDATE 1":
                return False
            await self._insert_event(connection, signal, event_type, event_at)
            return True

    async def load_signal(self, signal_id: str) -> Signal | None:
        """Load one exact immutable signal instance, including terminal rows."""
        async with self._pool.acquire() as connection:
            payload = await connection.fetchval(
                f"SELECT payload::text FROM {self._outcomes} WHERE signal_id = $1",
                signal_id,
            )
        if payload is None:
            return None
        try:
            return deserialize_signal(str(payload))
        except (KeyError, TypeError, ValueError):
            return None

    async def load_open_signals(self) -> tuple[Signal, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"SELECT payload::text AS payload FROM {self._outcomes} WHERE state = ANY($1::text[])",
                [state.value for state in OPEN_STATES | STOP_STATES],
            )
        signals: list[Signal] = []
        for row in rows:
            try:
                signal = deserialize_signal(str(row["payload"]))
                if signal.state in STOP_STATES and (
                    signal.stopped_then_target_reached
                    or signal.follow_up_until is None
                    or datetime.now(UTC) >= signal.follow_up_until
                ):
                    continue
                signals.append(signal)
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
                       stopped_at, invalidated_at, payload::text AS payload,
                       mode, mae, mfe, atr_at_entry, stopped_then_target_reached
                FROM {self._outcomes} WHERE created_at >= $1
                """,
                cutoff,
            )
        wins = sum(bool(row["win"]) for row in rows)
        losses = sum(row["state"] in {state.value for state in STOP_STATES} and not bool(row["win"]) for row in rows)
        durations: list[float] = []
        resolved_r: list[float] = []
        mae_atr: list[float] = []
        mfe_atr: list[float] = []
        winner_mae_atr: list[float] = []
        modes: dict[str, list[Any]] = {}
        dimension_rows: dict[str, dict[str, list[Any]]] = {
            "symbol": {},
            "strategy": {},
            "direction": {},
            "regime": {},
            "ai_verdict": {},
            "setup_score_band": {},
            "entry_score_band": {},
        }
        for row in rows:
            activated = row["activated_at"]
            resolved = row["tp1_hit_at"] if bool(row["win"]) else row["stopped_at"]
            if isinstance(activated, datetime) and isinstance(resolved, datetime) and resolved >= activated:
                durations.append((resolved - activated).total_seconds() / 3600)
            try:
                signal = deserialize_signal(str(row["payload"]))
            except (KeyError, TypeError, ValueError):
                continue
            modes.setdefault(signal.mode.value, []).append(row)
            dimensions = {
                "symbol": signal.symbol,
                "strategy": signal.strategy,
                "direction": signal.direction.value,
                "regime": signal.regime.value,
                "ai_verdict": signal.ai_review.verdict.value if signal.ai_review else "NOT_REVIEWED",
                "setup_score_band": f"{signal.score // 5 * 5}-{signal.score // 5 * 5 + 4}",
                "entry_score_band": (
                    f"{signal.entry_quality.total // 5 * 5}-{signal.entry_quality.total // 5 * 5 + 4}"
                    if signal.entry_quality
                    else "UNKNOWN"
                ),
            }
            for dimension, value in dimensions.items():
                dimension_rows[dimension].setdefault(value, []).append(row)
            if bool(row["win"]):
                target = signal.trade.tp2 if signal.tp2_hit_at else signal.trade.tp1
                resolved_r.append(abs(target - (signal.entry_trigger_price or signal.trade.preferred_entry)) / signal.trade.risk_per_unit)
            elif row["state"] in {state.value for state in STOP_STATES} and signal.activated_at is not None:
                resolved_r.append(-1.0)
            atr = float(row["atr_at_entry"]) if row["atr_at_entry"] else None
            if atr and atr > 0 and signal.activated_at is not None:
                normalized_mae = float(row["mae"]) / atr
                mae_atr.append(normalized_mae)
                mfe_atr.append(float(row["mfe"]) / atr)
                if bool(row["win"]):
                    winner_mae_atr.append(normalized_mae)
        by_mode: dict[str, dict[str, float | int | None]] = {}
        for mode, grouped in modes.items():
            mode_wins = sum(bool(row["win"]) for row in grouped)
            mode_losses = sum(row["state"] in {state.value for state in STOP_STATES} and not bool(row["win"]) for row in grouped)
            resolved = mode_wins + mode_losses
            by_mode[mode] = {"signals": len(grouped), "wins": mode_wins, "losses": mode_losses, "win_rate": mode_wins / resolved * 100 if resolved else None}
        breakdowns: dict[str, dict[str, dict[str, float | int | None]]] = {}
        for dimension, groups in dimension_rows.items():
            breakdowns[dimension] = {}
            for value, grouped in groups.items():
                group_wins = sum(bool(row["win"]) for row in grouped)
                group_losses = sum(row["state"] in {state.value for state in STOP_STATES} and not bool(row["win"]) for row in grouped)
                group_resolved = group_wins + group_losses
                breakdowns[dimension][value] = {"signals": len(grouped), "wins": group_wins, "losses": group_losses, "win_rate": group_wins / group_resolved * 100 if group_resolved else None}

        def percentile(values: list[float], quantile: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            index = (len(ordered) - 1) * quantile
            lower = int(index)
            upper = min(len(ordered) - 1, lower + 1)
            weight = index - lower
            return ordered[lower] * (1 - weight) + ordered[upper] * weight
        return PerformanceStats(
            tracking_since=cutoff,
            period_days=period_days,
            signals=len(rows),
            activated=sum(row["activated_at"] is not None for row in rows),
            wins=wins,
            losses=losses,
            open_signals=sum(
                row["state"] in {state.value for state in OPEN_STATES - {SignalState.TP1_HIT}}
                for row in rows
            ),
            tp1_runners=sum(row["state"] == SignalState.TP1_HIT.value for row in rows),
            tp2_hits=sum(row["tp2_hit_at"] is not None for row in rows),
            invalidated=sum(
                row["state"]
                in {
                    SignalState.INVALIDATED.value,
                    SignalState.MISSED.value,
                    SignalState.EXPIRED.value,
                }
                for row in rows
            ),
            average_hold_hours=sum(durations) / len(durations) if durations else None,
            average_r=sum(resolved_r) / len(resolved_r) if resolved_r else None,
            expectancy_r=sum(resolved_r) / len(resolved_r) if resolved_r else None,
            median_mae_atr=median(mae_atr) if mae_atr else None,
            p75_mae_atr=percentile(mae_atr, 0.75),
            p90_mae_atr=percentile(mae_atr, 0.90),
            median_mfe_atr=median(mfe_atr) if mfe_atr else None,
            average_mae_atr=sum(mae_atr) / len(mae_atr) if mae_atr else None,
            average_mfe_atr=sum(mfe_atr) / len(mfe_atr) if mfe_atr else None,
            winners_adverse_over_half_atr_pct=(
                sum(value > 0.5 for value in winner_mae_atr) / len(winner_mae_atr) * 100
                if winner_mae_atr
                else None
            ),
            stopped_then_target_reached=sum(bool(row["stopped_then_target_reached"]) for row in rows),
            ambiguous=sum(row["state"] == SignalState.AMBIGUOUS.value for row in rows),
            by_mode=by_mode,
            breakdowns=breakdowns,
        )

    async def _prune(self, connection: asyncpg.Connection) -> None:
        del connection
        # Durable performance history is append-only.  Memory remains bounded
        # independently by SignalStore.
        return

    async def close(self) -> None:
        await self._pool.close()
