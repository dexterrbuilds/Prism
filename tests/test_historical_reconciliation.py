from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.analysis.data_quality import TIMEFRAME_MS
from app.models import CandleSeries, SignalState
from app.signals.reconciliation import HistoricalSignalReconciler
from app.signals.repository import SQLiteOutcomeRepository
from tests.test_lifecycle import make_waiting_signal
from tests.test_signal_identity import _active


class FakeHistoricalMarket:
    def __init__(self, rows: dict[str, list[tuple[int, float, float, float]]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, int, int]] = []

    async def fetch_ohlcv_page(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        as_of_ms: int,
        *,
        limit: int = 250,
    ) -> CandleSeries:
        del symbol
        assert limit <= 250
        self.calls.append((timeframe, since_ms, limit))
        interval = TIMEFRAME_MS[timeframe]
        selected = [
            row
            for row in self.rows.get(timeframe, ())
            if row[0] >= since_ms and row[0] + interval <= as_of_ms
        ][:limit]
        timestamps = np.asarray([row[0] for row in selected], dtype=np.int64)
        highs = np.asarray([row[1] for row in selected], dtype=np.float64)
        lows = np.asarray([row[2] for row in selected], dtype=np.float64)
        closes = np.asarray([row[3] for row in selected], dtype=np.float64)
        return CandleSeries(
            "UNI/USDT",
            timeframe,
            timestamps,
            closes.copy(),
            highs,
            lows,
            closes,
            np.full(len(selected), 100.0),
            as_of_ms,
        )


def _execution_rows(
    started: datetime,
    count: int = 400,
    *,
    event_index: int | None = None,
    event_high: float = 101,
    event_low: float = 99,
) -> list[tuple[int, float, float, float]]:
    interval = TIMEFRAME_MS["15m"]
    rows: list[tuple[int, float, float, float]] = []
    for index in range(count):
        high = event_high if index == event_index else 101
        low = event_low if index == event_index else 99
        rows.append((int(started.timestamp() * 1000) + index * interval, high, low, 100))
    return rows


def _historical_active(signal_id: str, started: datetime):
    return replace(
        _active(signal_id),
        symbol="UNI/USDT",
        created_at=started,
        state_changed_at=started,
        activated_at=started,
        last_evaluated_at=started,
        trading_timeframe="15m",
    )


def _set_tracking_start(path: str, started: datetime) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE metadata SET value = ? WHERE key = 'tracking_started_at'",
        (started.isoformat(),),
    )
    connection.commit()
    connection.close()


@pytest.mark.asyncio
async def test_old_long_recovers_tp_beyond_normal_250_candle_window(tmp_path) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    rows = _execution_rows(started, event_index=100, event_high=106)
    path = str(tmp_path / "old-tp.db")
    repository = SQLiteOutcomeRepository(path)
    _set_tracking_start(path, started)
    signal = replace(
        _historical_active("old-long", started),
        state=SignalState.ENTRY_READY,
        activated_at=None,
        entry_trigger_price=None,
    )
    assert await repository.record_signal(signal)

    report = await HistoricalSignalReconciler(repository, FakeHistoricalMarket({"15m": rows})).reconcile(
        signal.id,
        lookback_hours=168,
        apply=True,
        now=started + timedelta(hours=100),
    )

    assert report.pages_fetched >= 2
    assert report.final_state is SignalState.TP1_HIT
    assert report.transitions[0] == SignalState.ENTRY_TRIGGERED.value
    assert "TP1_HIT" in report.transitions
    assert (await repository.stats()).wins == 1
    # The recovered event is older than the latest 250 execution candles.
    assert rows[100][0] < rows[-250][0]
    await repository.close()


@pytest.mark.asyncio
async def test_old_signal_that_never_triggered_is_not_counted(tmp_path) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    waiting = replace(
        make_waiting_signal(),
        id="never-entered",
        symbol="UNI/USDT",
        state=SignalState.ENTRY_READY,
        created_at=started,
        state_changed_at=started,
        last_evaluated_at=started,
        expires_at=started + timedelta(hours=6),
        max_missed_distance=None,
    )
    rows = [
        (int(started.timestamp() * 1000) + index * TIMEFRAME_MS["15m"], 104.0, 102.0, 103.0)
        for index in range(40)
    ]
    path = str(tmp_path / "never.db")
    repository = SQLiteOutcomeRepository(path)
    _set_tracking_start(path, started)
    assert await repository.record_signal(waiting)

    report = await HistoricalSignalReconciler(repository, FakeHistoricalMarket({"15m": rows})).reconcile(
        waiting.id,
        lookback_hours=12,
        apply=True,
        now=started + timedelta(hours=12),
    )

    assert report.final_state is SignalState.EXPIRED
    stats = await repository.stats()
    assert stats.activated == stats.wins == stats.losses == 0
    await repository.close()


@pytest.mark.asyncio
async def test_old_active_signal_recovers_stop_loss(tmp_path) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    path = str(tmp_path / "old-sl.db")
    repository = SQLiteOutcomeRepository(path)
    _set_tracking_start(path, started)
    signal = _historical_active("old-stop", started)
    assert await repository.record_signal(signal)
    rows = _execution_rows(started, count=30, event_index=20, event_low=94)

    report = await HistoricalSignalReconciler(repository, FakeHistoricalMarket({"15m": rows})).reconcile(
        signal.id,
        lookback_hours=12,
        apply=True,
        now=started + timedelta(hours=12),
    )

    assert report.final_state is SignalState.STOPPED
    stats = await repository.stats()
    assert stats.wins == 0
    assert stats.losses == 1
    await repository.close()


@pytest.mark.asyncio
async def test_same_candle_tp_sl_is_resolved_with_complete_finer_data(tmp_path) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    start_ms = int(started.timestamp() * 1000)
    parent = [(start_ms, 106.0, 94.0, 100.0)]
    finer = [
        (start_ms, 106.0, 100.0, 105.0),
        (start_ms + 300_000, 101.0, 94.0, 96.0),
        (start_ms + 600_000, 101.0, 99.0, 100.0),
    ]
    path = str(tmp_path / "resolved.db")
    repository = SQLiteOutcomeRepository(path)
    _set_tracking_start(path, started)
    signal = _historical_active("resolved", started)
    assert await repository.record_signal(signal)

    report = await HistoricalSignalReconciler(
        repository,
        FakeHistoricalMarket({"15m": parent, "5m": finer}),
    ).reconcile(signal.id, lookback_hours=1, apply=True, now=started + timedelta(hours=1))

    assert report.transitions == ("TP1_HIT", "STOPPED")
    assert report.final_state is SignalState.STOPPED
    stats = await repository.stats()
    assert stats.wins == 1
    assert stats.losses == 0
    await repository.close()


@pytest.mark.asyncio
async def test_unresolved_same_candle_tp_sl_remains_ambiguous(tmp_path) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    parent = [(int(started.timestamp() * 1000), 106.0, 94.0, 100.0)]
    path = str(tmp_path / "ambiguous.db")
    repository = SQLiteOutcomeRepository(path)
    _set_tracking_start(path, started)
    signal = _historical_active("unresolved", started)
    assert await repository.record_signal(signal)

    report = await HistoricalSignalReconciler(repository, FakeHistoricalMarket({"15m": parent})).reconcile(
        signal.id,
        lookback_hours=1,
        apply=True,
        now=started + timedelta(hours=1),
    )

    assert report.final_state is SignalState.AMBIGUOUS
    stats = await repository.stats()
    assert stats.wins == stats.losses == 0
    assert stats.ambiguous == 1
    await repository.close()


@pytest.mark.asyncio
async def test_reconciliation_is_idempotent_and_does_not_duplicate_win_or_event(tmp_path) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    path = str(tmp_path / "twice.db")
    repository = SQLiteOutcomeRepository(path)
    _set_tracking_start(path, started)
    signal = _historical_active("twice", started)
    assert await repository.record_signal(signal)
    source = FakeHistoricalMarket({"15m": _execution_rows(started, count=20, event_index=5, event_high=106)})
    reconciler = HistoricalSignalReconciler(repository, source)

    first = await reconciler.reconcile(
        signal.id,
        lookback_hours=5,
        apply=True,
        now=started + timedelta(hours=5),
    )
    second = await HistoricalSignalReconciler(repository, source).reconcile(
        signal.id,
        lookback_hours=5,
        apply=True,
        now=started + timedelta(hours=5),
    )

    assert first.transitions == ("TP1_HIT",)
    assert second.transitions == ()
    assert (await repository.stats()).wins == 1
    connection = sqlite3.connect(path)
    count = connection.execute(
        "SELECT COUNT(*) FROM signal_events WHERE signal_id = ? AND event_type = 'TP1_HIT'",
        (signal.id,),
    ).fetchone()[0]
    connection.close()
    assert count == 1
    await repository.close()


@pytest.mark.asyncio
async def test_only_explicitly_requested_signal_is_modified(tmp_path) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    path = str(tmp_path / "isolated.db")
    repository = SQLiteOutcomeRepository(path)
    _set_tracking_start(path, started)
    first = _historical_active("requested", started)
    second = _historical_active("untouched", started)
    assert await repository.record_signal(first)
    assert await repository.record_signal(second)
    source = FakeHistoricalMarket({"15m": _execution_rows(started, count=20, event_index=5, event_high=106)})

    await HistoricalSignalReconciler(repository, source).reconcile(
        first.id,
        lookback_hours=5,
        apply=True,
        now=started + timedelta(hours=5),
    )

    persisted_first = await repository.load_signal(first.id)
    persisted_second = await repository.load_signal(second.id)
    assert persisted_first is not None and persisted_first.state is SignalState.TP1_HIT
    assert persisted_second is not None and persisted_second.state is SignalState.ACTIVE
    await repository.close()
