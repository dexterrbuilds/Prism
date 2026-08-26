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
    connection.commit()
    connection.close()

    ledger = OutcomeLedger(str(path))
    migrated = sqlite3.connect(path)
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(signal_outcomes)")}
    migrated.close()
    ledger.close()

    assert {"expires_at", "entry_trigger_price", "missed_at", "expired_at", "lifecycle_reason"} <= columns


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
