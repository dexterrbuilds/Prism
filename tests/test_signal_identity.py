from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.api.health import RuntimeHealth
from app.config import Settings
from app.models import CandleSeries, Direction, EntryDecision, EntryQuality, PublicationState, SignalMode, SignalState, TradePlan
from app.scanner import Scanner
from app.signals.lifecycle import SignalStore, build_setup_fingerprint, create_signal_id, transition
from app.signals.outcomes import OutcomeLedger
from app.signals.repository import SQLiteOutcomeRepository
from tests.test_lifecycle import make_signal, make_waiting_signal
from tests.test_scanner_smoke import FakeTelegram


def _active(signal_id: str, *, entry: float = 100, tp1: float = 105, tp2: float = 110):
    started = datetime(2030, 8, 31, 20, 0, tzinfo=UTC)
    base = make_signal(state=SignalState.ACTIVE, entry=entry)
    risk = max((tp1 - entry) / 2, entry * 0.01)
    plan = replace(
        base.trade,
        entry_zone_low=entry - 1,
        entry_zone_high=entry + 1,
        preferred_entry=entry,
        stop_loss=entry - risk,
        risk_per_unit=risk,
        stop_distance_atr=1.5,
        invalidation_level=entry - risk,
        tp1=tp1,
        tp2=tp2,
    )
    return replace(
        base,
        id=signal_id,
        trade=plan,
        created_at=started,
        state_changed_at=started,
        activated_at=started,
        current_price=entry,
        setup_fingerprint=f"setup-{signal_id}",
        atr_at_entry=risk / 1.5,
    )


def test_two_same_symbol_active_instances_both_reach_their_own_targets() -> None:
    store = SignalStore()
    first = _active("A", entry=7.50, tp1=7.80, tp2=7.90)
    second = _active("B", entry=7.52, tp1=7.81, tp2=7.91)
    store.restore(first)
    store.restore(second)

    timestamp = int(first.created_at.timestamp() * 1000)
    events = store.track_candles("BTC/USDT", [timestamp], [7.82], [7.49], [7.82])

    assert {(event.id, event.state) for event in events} == {
        ("A", SignalState.TP1_HIT),
        ("B", SignalState.TP1_HIT),
    }


def test_newer_same_direction_signal_never_overwrites_older_instance() -> None:
    store = SignalStore()
    store.restore(_active("A"))
    store.restore(_active("B", entry=102, tp1=107, tp2=112))

    assert {signal.id for signal in store.signals_for_symbol("BTC/USDT")} == {"A", "B"}
    assert len(store) == 2


def test_issued_signal_ids_are_unique_even_with_identical_timestamp_and_market() -> None:
    created = datetime(2030, 8, 31, 20, 0, tzinfo=UTC)
    first = create_signal_id("UNI/USDT", _active("A").direction, created)
    second = create_signal_id("UNI/USDT", _active("B").direction, created)

    assert first != second
    assert first.startswith("SIG-UNI-L-20300831T200000")


def test_setup_fingerprint_is_stable_but_changes_for_new_structure_origin() -> None:
    values = {
        "symbol": "UNI/USDT",
        "direction": Direction.LONG,
        "mode": SignalMode.INTRADAY,
        "strategy": "BREAKOUT_RETEST",
        "regime": "BULLISH_TREND",
        "entry_low": 7.49,
        "entry_high": 7.53,
        "invalidation": 7.35,
        "major_structure_level": 7.50,
        "atr": 0.10,
        "setup_origin_ms": 1_777_000_000_000,
    }
    first = build_setup_fingerprint(**values)  # type: ignore[arg-type]
    repeated = build_setup_fingerprint(**values)  # type: ignore[arg-type]
    second = build_setup_fingerprint(**(values | {"setup_origin_ms": 1_777_003_600_000}))  # type: ignore[arg-type]

    assert first == repeated
    assert first != second


def test_duplicate_scan_is_suppressed_while_original_setup_is_open() -> None:
    store = SignalStore()
    first = replace(_active("A"), setup_fingerprint="stable-setup")
    duplicate = replace(
        first,
        id="B",
        created_at=first.created_at + timedelta(minutes=45),
        trade=replace(first.trade, preferred_entry=100.05),
    )
    store.restore(first)

    match = store.find_duplicate(duplicate)

    assert match is not None
    assert match.signal.id == "A"
    assert not store.should_publish(duplicate)


def test_active_materially_same_setup_is_suppressed_even_after_time_window() -> None:
    store = SignalStore()
    first = replace(_active("A"), setup_fingerprint="")
    duplicate = replace(
        first,
        id="B",
        created_at=first.created_at + timedelta(hours=8),
        trade=replace(first.trade, preferred_entry=100.05),
    )
    store.restore(first)

    assert store.find_duplicate(duplicate, window_minutes=360) is not None


def test_new_confirmed_structural_origin_allows_distinct_reentry() -> None:
    store = SignalStore()
    first = replace(
        _active("A"),
        setup_fingerprint="origin-one",
        setup_origin_at=datetime(2030, 8, 31, 19, 0, tzinfo=UTC),
    )
    second = replace(
        first,
        id="B",
        setup_fingerprint="origin-two",
        setup_origin_at=datetime(2030, 8, 31, 20, 0, tzinfo=UTC),
        created_at=first.created_at + timedelta(minutes=45),
    )
    store.restore(first)

    assert store.find_duplicate(second) is None
    assert store.should_publish(second)


def test_same_waiting_setup_seven_hours_later_is_not_new_only_because_window_elapsed() -> None:
    store = SignalStore()
    origin = datetime(2030, 8, 31, 18, 0, tzinfo=UTC)
    first = replace(
        make_waiting_signal(),
        id="A",
        setup_fingerprint="",
        setup_origin_at=origin,
        major_structure_level=100,
        atr_at_entry=2,
    )
    repeated = replace(
        first,
        id="B",
        created_at=first.created_at + timedelta(hours=7),
        state_changed_at=first.created_at + timedelta(hours=7),
        trade=replace(first.trade, preferred_entry=100.05),
    )
    store.restore(first)

    match = store.find_duplicate(repeated, window_minutes=360)

    assert match is not None
    assert "outside the supporting time window" in match.reason


@pytest.mark.parametrize("minutes", [20, 90])
def test_new_structural_origin_is_allowed_inside_360_minute_window(minutes: int) -> None:
    store = SignalStore()
    first = replace(
        _active("A"),
        setup_fingerprint="first-origin",
        setup_origin_at=datetime(2030, 8, 31, 19, 0, tzinfo=UTC),
        strategy="BREAKOUT_RETEST",
    )
    second = replace(
        first,
        id="B",
        created_at=first.created_at + timedelta(minutes=minutes),
        setup_fingerprint="second-origin",
        setup_origin_at=first.setup_origin_at + timedelta(minutes=minutes),  # type: ignore[operator]
        strategy="LIQUIDITY_SWEEP_REVERSAL" if minutes == 20 else "BREAKOUT_RETEST",
    )
    store.restore(first)

    assert store.find_duplicate(second, window_minutes=360) is None


def test_different_strategy_label_on_same_geometry_is_not_automatically_a_new_trade() -> None:
    store = SignalStore()
    origin = datetime(2030, 8, 31, 19, 0, tzinfo=UTC)
    first = replace(_active("A"), setup_fingerprint="", setup_origin_at=origin)
    relabelled = replace(
        first,
        id="B",
        strategy="LIQUIDITY_SWEEP_REVERSAL",
        created_at=first.created_at + timedelta(minutes=30),
    )
    store.restore(first)

    assert store.find_duplicate(relabelled) is not None


def test_different_strategy_and_material_structure_is_evaluated_as_new() -> None:
    store = SignalStore()
    first = replace(
        _active("A"),
        setup_fingerprint="one",
        setup_origin_at=datetime(2030, 8, 31, 19, 0, tzinfo=UTC),
        major_structure_level=100,
    )
    second = replace(
        first,
        id="B",
        strategy="LIQUIDITY_SWEEP_REVERSAL",
        setup_fingerprint="two",
        setup_origin_at=datetime(2030, 8, 31, 20, 20, tzinfo=UTC),
        major_structure_level=104,
        created_at=first.created_at + timedelta(minutes=20),
    )
    store.restore(first)

    assert store.find_duplicate(second) is None


def test_confirmed_new_pullback_is_classified_as_reentry_with_parent() -> None:
    store = SignalStore()
    first = replace(
        _active("A"),
        setup_origin_at=datetime(2030, 8, 31, 19, 0, tzinfo=UTC),
        major_structure_level=100,
    )
    quality = EntryQuality(
        total=82,
        decision=EntryDecision.VALID,
        categories={},
        evidence=("new pullback confirmed",),
        retest_completed=True,
        lower_timeframe_confirmed=True,
    )
    reentry = replace(
        first,
        id="B",
        state=SignalState.ENTRY_READY,
        created_at=first.created_at + timedelta(minutes=30),
        setup_origin_at=datetime(2030, 8, 31, 20, 30, tzinfo=UTC),
        major_structure_level=103,
        entry_quality=quality,
    )
    store.restore(first)

    parent = store.find_reentry_parent(reentry)

    assert parent is not None
    assert parent.id == first.id
    issued = replace(reentry, signal_type="RE_ENTRY", parent_signal_id=parent.id)
    assert issued.id != parent.id
    assert issued.signal_type == "RE_ENTRY"


def test_only_the_independently_triggered_signal_counts_as_a_win(tmp_path) -> None:
    ledger = OutcomeLedger(str(tmp_path / "independent.db"))
    waiting = replace(make_waiting_signal(), id="A")
    active = _active("B")
    assert ledger.record_signal(waiting)
    assert ledger.record_signal(active)
    tp1 = transition(
        active,
        SignalState.TP1_HIT,
        current_price=active.trade.tp1,
        changed_at=active.created_at + timedelta(minutes=15),
    )
    assert ledger.record_event(tp1)

    stats = ledger.stats(now=active.created_at + timedelta(hours=1))
    assert stats.signals == 2
    assert stats.activated == 1
    assert stats.wins == 1
    assert stats.losses == 0
    assert stats.resolved == 1
    ledger.close()


def test_same_candle_crossing_two_targets_resolves_both_instances() -> None:
    store = SignalStore()
    first = _active("A", tp1=104, tp2=108)
    second = _active("B", tp1=105, tp2=109)
    store.restore(first)
    store.restore(second)
    timestamp = int(first.created_at.timestamp() * 1000)

    events = store.track_candles("BTC/USDT", [timestamp], [109.5], [99], [108])

    assert {(event.id, event.state) for event in events} == {
        ("A", SignalState.TP2_HIT),
        ("B", SignalState.TP2_HIT),
    }


def test_same_candle_stop_and_target_is_terminal_ambiguous_not_a_win(tmp_path) -> None:
    ledger = OutcomeLedger(str(tmp_path / "ambiguous.db"))
    active = _active("ambiguous")
    assert ledger.record_signal(active)
    store = SignalStore()
    store.restore(active)
    timestamp = int(active.created_at.timestamp() * 1000)

    events = store.track_candles("BTC/USDT", [timestamp], [106], [94], [101])

    assert len(events) == 1
    ambiguous = events[0]
    assert ambiguous.state is SignalState.AMBIGUOUS
    assert ambiguous.terminal_state == SignalState.AMBIGUOUS.value
    assert ambiguous.result == "AMBIGUOUS"
    assert ledger.record_event(ambiguous)
    stats = ledger.stats(now=active.created_at + timedelta(hours=1))
    assert stats.wins == 0
    assert stats.losses == 0
    assert stats.ambiguous == 1
    ledger.close()


def test_restart_restores_every_same_symbol_instance(tmp_path) -> None:
    path = str(tmp_path / "restart.db")
    ledger = OutcomeLedger(path)
    assert ledger.record_signal(_active("A"))
    assert ledger.record_signal(_active("B", entry=102, tp1=107, tp2=112))
    ledger.close()

    reopened = OutcomeLedger(path)
    restored = reopened.load_open_signals()
    store = SignalStore()
    for signal in restored:
        store.restore(signal)

    assert {signal.id for signal in store.signals_for_symbol("BTC/USDT")} == {"A", "B"}
    reopened.close()


def test_downtime_candle_replay_recovers_missed_tp_idempotently(tmp_path) -> None:
    path = str(tmp_path / "reconcile.db")
    ledger = OutcomeLedger(path)
    active = _active("offline")
    assert ledger.record_signal(active)
    ledger.close()

    reopened = OutcomeLedger(path)
    restored = reopened.load_open_signals()[0]
    store = SignalStore()
    store.restore(restored)
    timestamp = int(active.created_at.timestamp() * 1000)
    first = store.track_candles("BTC/USDT", [timestamp], [106], [99], [105])
    second = store.track_candles("BTC/USDT", [timestamp], [106], [99], [105])

    assert [event.state for event in first] == [SignalState.TP1_HIT]
    assert second == []
    assert reopened.record_event(first[0])
    assert not reopened.record_event(first[0])
    assert reopened.stats(now=active.created_at + timedelta(hours=1)).wins == 1
    reopened.close()


@pytest.mark.asyncio
async def test_scanner_startup_reconciliation_repairs_all_persisted_instances(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    path = str(tmp_path / "startup-reconcile.db")
    repository = SQLiteOutcomeRepository(path)
    started = datetime.now(UTC) - timedelta(minutes=31)
    metadata = sqlite3.connect(path)
    metadata.execute(
        "UPDATE metadata SET value = ? WHERE key = 'tracking_started_at'",
        ((started - timedelta(minutes=1)).isoformat(),),
    )
    metadata.commit()
    metadata.close()
    first = replace(
        _active("A"),
        created_at=started,
        state_changed_at=started,
        activated_at=started,
        publication_state=PublicationState.PUBLISHED,
        published_at=started,
    )
    second = replace(
        _active("B", tp1=105.5),
        created_at=started,
        state_changed_at=started,
        activated_at=started,
        publication_state=PublicationState.PUBLISHED,
        published_at=started,
    )
    assert await repository.record_signal(first)
    assert await repository.record_signal(second)

    class ReconciliationExchange:
        async def fetch_ohlcv(self, symbol: str, timeframe: str, as_of_ms: int) -> CandleSeries:
            assert timeframe == "15m"
            interval = 900_000
            timestamps = as_of_ms - np.arange(250, 0, -1, dtype=np.int64) * interval
            close = np.full(250, 100.0)
            high = np.full(250, 101.0)
            low = np.full(250, 99.0)
            high[-1] = 106.0
            close[-1] = 105.75
            return CandleSeries(
                symbol,
                timeframe,
                timestamps,
                close.copy(),
                high,
                low,
                close,
                np.full(250, 100.0),
                as_of_ms,
            )

    health = RuntimeHealth("fake")
    telegram = FakeTelegram()
    scanner = Scanner(
        Settings.from_env(),
        ReconciliationExchange(),  # type: ignore[arg-type]
        telegram,
        health,
        repository,
    )
    await scanner.restore_outcomes()

    repaired = await scanner.reconcile_open_signals(startup=True)

    assert repaired == 2
    assert health.orphaned_signals_reconciled == 2
    assert {(signal.id, signal.state) for signal in telegram.signals} == {
        ("A", SignalState.TP1_HIT),
        ("B", SignalState.TP1_HIT),
    }
    stats = await repository.stats()
    assert stats.wins == 2
    await repository.close()


def test_event_log_contains_one_idempotent_event_per_signal_state(tmp_path) -> None:
    path = str(tmp_path / "events.db")
    ledger = OutcomeLedger(path)
    active = _active("evented")
    assert ledger.record_signal(active)
    tp1 = transition(active, SignalState.TP1_HIT, changed_at=active.created_at + timedelta(minutes=15))
    assert ledger.record_event(tp1)
    assert not ledger.record_event(tp1)
    ledger.close()

    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT event_type FROM signal_events WHERE signal_id = ? ORDER BY event_at",
        (active.id,),
    ).fetchall()
    connection.close()
    assert rows == [("CREATED",), ("TP1_HIT",)]


def test_trade_plan_helper_keeps_structural_geometry() -> None:
    # Guards the fixture itself: all identity tests must use an actual 2R plan.
    signal = _active("geometry")
    assert isinstance(signal.trade, TradePlan)
    assert signal.trade.stop_loss < signal.trade.preferred_entry < signal.trade.tp1 < signal.trade.tp2
