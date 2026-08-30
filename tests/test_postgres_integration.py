from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.models import SignalState
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
        winner = transition(active, SignalState.TP1_HIT, current_price=105, changed_at=started + timedelta(minutes=5))
        assert await repository.record_event(winner)
        assert await repository.record_observation(replace(winner, mae=1.5, mfe=5.0))
        stats = await repository.stats()
        assert stats.wins >= 1
        assert stats.by_mode["INTRADAY"]["wins"] >= 1
    finally:
        await repository.close()
