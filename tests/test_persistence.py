from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.config import Settings
from app.models import SignalState
from app.signals.lifecycle import transition
from app.signals.outcomes import OutcomeLedger
from app.signals.postgres_outcomes import PostgresOutcomeRepository
from app.signals.repository import SQLiteOutcomeRepository
from tests.test_lifecycle import make_signal


def test_database_url_auto_selects_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/postgres")
    monkeypatch.delenv("OUTCOME_BACKEND", raising=False)
    settings = Settings.from_env()
    settings.validate()
    assert settings.resolved_outcome_backend == "postgres"


def test_explicit_postgres_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTCOME_BACKEND", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings.from_env().validate()


def test_lifecycle_monitor_cadence_is_environment_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIFECYCLE_MONITOR_SECONDS", "45")
    settings = Settings.from_env()
    assert settings.lifecycle_monitor_seconds == 45


def test_signal_similarity_thresholds_are_environment_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNAL_DEDUP_WINDOW_MINUTES", "180")
    monkeypatch.setenv("SIGNAL_DEDUP_ENTRY_ATR", "0.15")
    monkeypatch.setenv("SIGNAL_DEDUP_STOP_ATR", "0.20")
    monkeypatch.setenv("SIGNAL_DEDUP_TARGET_ATR", "0.30")
    settings = Settings.from_env()
    assert settings.signal_dedup_window_minutes == 180
    assert settings.signal_dedup_entry_atr == 0.15
    assert settings.signal_dedup_stop_atr == 0.20
    assert settings.signal_dedup_target_atr == 0.30


def test_existing_sqlite_database_is_migrated_additively(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE signal_outcomes (
            signal_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            state TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
            direction TEXT NOT NULL, score INTEGER NOT NULL, current_price REAL,
            activated_at TEXT, tp1_hit_at TEXT, tp2_hit_at TEXT, stopped_at TEXT,
            invalidated_at TEXT, win INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO signal_outcomes(
            signal_id, created_at, updated_at, state, symbol, strategy,
            direction, score, win, payload
        ) VALUES ('legacy-id', '2026-08-01T00:00:00+00:00',
                  '2026-08-01T01:00:00+00:00', 'ACTIVE', 'UNI/USDT',
                  'BREAKOUT_RETEST', 'LONG', 85, 0, '{}')
        """
    )
    connection.commit()
    connection.close()

    ledger = OutcomeLedger(str(path))
    migrated = sqlite3.connect(path)
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(signal_outcomes)")}
    tables = {row[0] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    preserved = migrated.execute(
        "SELECT signal_id, symbol, state FROM signal_outcomes WHERE signal_id = 'legacy-id'"
    ).fetchone()
    migrated_event = migrated.execute(
        "SELECT event_type FROM signal_events WHERE signal_id = 'legacy-id'"
    ).fetchone()
    migrated.close()
    ledger.close()

    assert {
        "expires_at",
        "entry_trigger_price",
        "missed_at",
        "expired_at",
        "lifecycle_reason",
        "mode",
        "entry_quality_score",
        "atr_at_entry",
        "mae",
        "mfe",
        "stopped_then_target_reached",
        "follow_up_until",
        "setup_fingerprint",
        "signal_type",
        "parent_signal_id",
        "setup_origin_at",
        "major_structure_level",
        "last_evaluated_at",
        "terminal_state",
        "terminal_at",
        "result",
        "publication_state",
        "published_at",
        "channel_published_at",
        "channel_message_id",
        "dm_delivery_attempted_at",
        "dm_success_count",
        "dm_failure_count",
        "publish_attempts",
        "last_publish_error",
    } <= columns
    assert "signal_events" in tables
    assert preserved == ("legacy-id", "UNI/USDT", "ACTIVE")
    assert migrated_event == ("MIGRATED_SNAPSHOT",)
    assert len(list(tmp_path.glob("legacy.db.pre_identity_v3.*.bak"))) == 1


def test_sqlite_publication_only_migration_creates_backup(tmp_path) -> None:
    path = tmp_path / "pre-publication.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE signal_outcomes (
            signal_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            state TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
            direction TEXT NOT NULL, score INTEGER NOT NULL, current_price REAL,
            activated_at TEXT, tp1_hit_at TEXT, tp2_hit_at TEXT, stopped_at TEXT,
            invalidated_at TEXT, win INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
            setup_fingerprint TEXT, signal_type TEXT NOT NULL DEFAULT 'INITIAL',
            parent_signal_id TEXT, setup_origin_at TEXT, major_structure_level REAL,
            last_evaluated_at TEXT, terminal_state TEXT, terminal_at TEXT, result TEXT
        )
        """
    )
    connection.commit()
    connection.close()

    ledger = OutcomeLedger(str(path))
    ledger.close()

    assert list(tmp_path.glob("pre-publication.db.pre_identity_v3.*.bak")) == []
    assert len(list(tmp_path.glob("pre-publication.db.pre_publication_v4.*.bak"))) == 1


@pytest.mark.asyncio
async def test_async_sqlite_adapter_persists_win_across_restart(tmp_path) -> None:
    path = str(tmp_path / "signals.db")
    repository = SQLiteOutcomeRepository(path)
    started = datetime.now(UTC)
    active = replace(
        make_signal(state=SignalState.ACTIVE),
        id="async-winner",
        created_at=started,
        state_changed_at=started,
        activated_at=started,
    )
    assert await repository.record_signal(active)
    assert await repository.record_event(
        transition(active, SignalState.TP1_HIT, current_price=105, changed_at=started + timedelta(hours=1))
    )
    await repository.close()

    reopened = SQLiteOutcomeRepository(path)
    stats = await reopened.stats()
    assert stats.wins == 1
    assert stats.losses == 0
    assert stats.win_rate == 100
    await reopened.close()


@pytest.mark.asyncio
async def test_repository_load_signal_uses_exact_immutable_id(tmp_path) -> None:
    repository = SQLiteOutcomeRepository(str(tmp_path / "exact.db"))
    first = replace(make_signal(), id="first")
    second = replace(make_signal(), id="second")
    assert await repository.record_signal(first)
    assert await repository.record_signal(second)

    loaded = await repository.load_signal("first")

    assert loaded is not None
    assert loaded.id == "first"
    assert await repository.load_signal("missing") is None
    await repository.close()


@pytest.mark.asyncio
async def test_supabase_pool_uses_tls_and_disables_statement_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class AcquireContext:
        async def __aenter__(self) -> FakeConnection:
            return FakeConnection()

        async def __aexit__(self, *args: object) -> None:
            del args

    class FakeConnection:
        async def execute(self, query: str, *args: object) -> str:
            calls.setdefault("queries", []).append((query, args))
            return "OK"

    class FakePool:
        def acquire(self) -> AcquireContext:
            return AcquireContext()

        async def close(self) -> None:
            calls["closed"] = True

    async def fake_create_pool(**kwargs: object) -> FakePool:
        calls["pool"] = kwargs
        return FakePool()

    monkeypatch.setattr("app.signals.postgres_outcomes.asyncpg.create_pool", fake_create_pool)
    repository = await PostgresOutcomeRepository.create(
        "postgresql://redacted@pooler.example:5432/postgres",
        pool_min=1,
        pool_max=3,
        ssl_required=True,
    )

    assert calls["pool"]["ssl"] == "require"
    assert calls["pool"]["statement_cache_size"] == 0
    assert calls["pool"]["min_size"] == 1
    assert calls["pool"]["max_size"] == 3
    assert any("CREATE SCHEMA" in query for query, _ in calls["queries"])
    assert any("signal_outcomes" in query for query, _ in calls["queries"])
    await repository.close()
    assert calls["closed"] is True
