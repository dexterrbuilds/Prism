from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import asyncpg  # type: ignore[import-untyped]
import pytest

from app.models import PublicationState, SignalState
from app.signals.lifecycle import transition
from app.signals.postgres_outcomes import PostgresOutcomeRepository
from tests.test_lifecycle import make_signal


@pytest.mark.asyncio
async def test_postgres_v2_schema_and_outcome_round_trip() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    repository = await PostgresOutcomeRepository.create(
        dsn,
        schema="prism_test",
        pool_min=1,
        pool_max=1,
        ssl_required=False,
    )
    started = datetime.now(UTC)
    active = replace(
        make_signal(state=SignalState.ACTIVE),
        id=f"postgres-v2-{started.timestamp()}",
        created_at=started,
        state_changed_at=started,
        activated_at=started,
        atr_at_entry=2.0,
        mae=1.0,
        mfe=2.0,
    )
    try:
        assert await repository.record_signal(active)
        published = replace(
            active,
            publication_state=PublicationState.PUBLISHED,
            published_at=started + timedelta(seconds=1),
            channel_published_at=started + timedelta(seconds=1),
            channel_message_id="123",
            intended_destination_ids=("-100123",),
            delivered_destination_ids=("-100123",),
            publish_attempts=1,
        )
        assert await repository.record_publication(published)
        winner = transition(published, SignalState.TP1_HIT, current_price=105, changed_at=started + timedelta(minutes=5))
        assert await repository.record_event(winner)
        assert await repository.record_observation(replace(winner, mae=1.5, mfe=5.0))
        loaded = await repository.load_signal(active.id)
        assert loaded is not None
        assert loaded.id == active.id
        assert loaded.state is SignalState.TP1_HIT
        assert loaded.publication_state is PublicationState.PUBLISHED
        assert loaded.channel_message_id == "123"
        stats = await repository.stats()
        assert stats.wins >= 1
        assert stats.by_mode["INTRADAY"]["wins"] >= 1
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_postgres_identity_migration_is_additive_and_creates_backup() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    connection = await asyncpg.connect(dsn, ssl=None)
    try:
        await connection.execute("DROP SCHEMA IF EXISTS prism_identity_migration_test CASCADE")
        await connection.execute("CREATE SCHEMA prism_identity_migration_test")
        await connection.execute(
            """
            CREATE TABLE prism_identity_migration_test.signal_outcomes (
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
            )
            """
        )
        await connection.execute(
            """
            INSERT INTO prism_identity_migration_test.signal_outcomes(
                signal_id, created_at, updated_at, state, symbol, strategy,
                direction, score, payload
            ) VALUES ('legacy-id', NOW(), NOW(), 'ACTIVE', 'UNI/USDT',
                      'BREAKOUT_RETEST', 'LONG', 85, '{}'::jsonb)
            """
        )
    finally:
        await connection.close()

    repository = await PostgresOutcomeRepository.create(
        dsn,
        schema="prism_identity_migration_test",
        pool_min=1,
        pool_max=1,
        ssl_required=False,
    )
    await repository.close()

    connection = await asyncpg.connect(dsn, ssl=None)
    try:
        columns = {
            str(row["column_name"])
            for row in await connection.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'prism_identity_migration_test'
                  AND table_name = 'signal_outcomes'
                """
            )
        }
        backup_count = await connection.fetchval(
            "SELECT COUNT(*) FROM prism_identity_migration_test.signal_outcomes_pre_identity_v3_backup"
        )
        publication_backup_count = await connection.fetchval(
            "SELECT COUNT(*) FROM prism_identity_migration_test.signal_outcomes_pre_publication_v4_backup"
        )
        event_count = await connection.fetchval(
            "SELECT COUNT(*) FROM prism_identity_migration_test.signal_events WHERE signal_id = 'legacy-id'"
        )
        assert {
            "setup_fingerprint",
            "last_evaluated_at",
            "terminal_state",
            "result",
            "publication_state",
            "published_at",
            "channel_message_id",
        } <= columns
        assert backup_count == 1
        assert publication_backup_count == 1
        assert event_count == 1
    finally:
        await connection.execute("DROP SCHEMA prism_identity_migration_test CASCADE")
        await connection.close()
