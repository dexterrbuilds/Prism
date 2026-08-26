from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Direction, MarketRegime, Signal, SignalGrade, SignalState, TradePlan
from app.signals.lifecycle import SignalStore, transition


def make_signal(*, score: int = 85, state: SignalState = SignalState.ACTIVE, entry: float = 100) -> Signal:
    trade = TradePlan(99, 101, entry, "retest", "hold", 95, 5, 2.5, "below structure", 105, 110, None, 2)
    return Signal("id", "BTC/USDT", "BREAKOUT_RETEST", Direction.LONG, MarketRegime.BULLISH_TREND, score, SignalGrade.VALID, state, trade, ("evidence",), datetime.now(UTC))


def make_waiting_signal(*, direction: Direction = Direction.LONG) -> Signal:
    created = datetime(2030, 8, 26, 12, 0, tzinfo=UTC)
    trade = TradePlan(
        99,
        101,
        100,
        "breakout retest",
        "15M confirmation",
        95 if direction is Direction.LONG else 105,
        5,
        2.5,
        "structure invalidation",
        105 if direction is Direction.LONG else 95,
        110 if direction is Direction.LONG else 90,
        None,
        2,
        invalidation_level=95 if direction is Direction.LONG else 105,
    )
    return Signal(
        "setup-id",
        "BTC/USDT",
        "BREAKOUT_RETEST",
        direction,
        MarketRegime.BULLISH_TREND,
        85,
        SignalGrade.VALID,
        SignalState.WAITING_ENTRY,
        trade,
        ("evidence",),
        created,
        current_price=102 if direction is Direction.LONG else 98,
        state_changed_at=created,
        expires_at=created + timedelta(hours=6),
        validity_minutes=360,
        valid_conditions=("Price stays beyond invalidation", "Entry remains actionable", "Setup has not expired"),
        max_missed_distance=1,
    )


def test_deduplication_requires_meaningful_change() -> None:
    store = SignalStore()
    assert store.should_publish(make_signal())
    assert not store.should_publish(make_signal())
    assert store.should_publish(make_signal(score=90))


def test_deduplication_collapses_strategy_labels_for_same_direction() -> None:
    store = SignalStore()
    first = make_signal()
    overlapping = replace(first, strategy="BOS_CONTINUATION", id="second")
    assert store.should_publish(first)
    assert not store.should_publish(overlapping)


def test_created_and_waiting_are_deduplicated_as_the_same_setup() -> None:
    store = SignalStore()
    created = replace(make_waiting_signal(), state=SignalState.CREATED)
    waiting = replace(created, state=SignalState.WAITING_ENTRY)
    assert store.should_publish(created)
    assert not store.should_publish(waiting)


def test_signal_state_transitions_are_guarded() -> None:
    detected = make_signal(state=SignalState.DETECTED)
    watching = transition(detected, SignalState.WATCHING)
    confirmed = transition(watching, SignalState.CONFIRMED)
    active = transition(confirmed, SignalState.ACTIVE)
    assert transition(active, SignalState.TP1_HIT).state is SignalState.TP1_HIT
    with pytest.raises(ValueError):
        transition(detected, SignalState.TP2_HIT)


def test_setup_created_can_enter_waiting_state() -> None:
    created = replace(make_waiting_signal(), state=SignalState.CREATED)
    waiting = transition(created, SignalState.WAITING_ENTRY, changed_at=created.created_at)
    assert waiting.state is SignalState.WAITING_ENTRY
    assert waiting.created_at == created.created_at


def test_price_tracking_emits_tp_and_stop_events() -> None:
    store = SignalStore()
    signal = make_signal()
    assert store.should_publish(signal)
    events = store.track_price("BTC/USDT", 105)
    assert events[-1].state is SignalState.TP1_HIT
    events = store.track_price("BTC/USDT", 110)
    assert events[-1].state is SignalState.TP2_HIT


def test_confirmed_signal_activates_only_inside_entry_zone() -> None:
    store = SignalStore()
    signal = make_signal(state=SignalState.CONFIRMED)
    assert store.should_publish(signal)
    assert store.track_price("BTC/USDT", 103) == []
    assert store.track_price("BTC/USDT", 100)[-1].state is SignalState.ACTIVE


def test_waiting_setup_entry_touch_triggers_once() -> None:
    store = SignalStore()
    signal = make_waiting_signal()
    store.restore(signal)
    triggered_at = signal.created_at + timedelta(minutes=5)

    first = store.track_price("BTC/USDT", 100, observed_at=triggered_at)
    second = store.track_price("BTC/USDT", 100, observed_at=triggered_at + timedelta(minutes=1))

    assert [event.state for event in first] == [SignalState.ENTRY_TRIGGERED]
    assert first[0].entry_trigger_price == 100
    assert first[0].activated_at == triggered_at
    assert second == []


def test_waiting_setup_becomes_missed_beyond_actionable_distance() -> None:
    store = SignalStore()
    signal = make_waiting_signal()
    store.restore(signal)

    events = store.track_price("BTC/USDT", 103, observed_at=signal.created_at + timedelta(minutes=5))

    assert events[-1].state is SignalState.MISSED
    assert events[-1].missed_at is not None
    assert "actionable entry area" in (events[-1].lifecycle_reason or "")
    assert store.track_price("BTC/USDT", 100, observed_at=signal.created_at + timedelta(minutes=6)) == []


def test_waiting_setup_invalidates_at_thesis_level_and_cannot_trigger_later() -> None:
    store = SignalStore()
    signal = replace(make_waiting_signal(), current_price=98)
    store.restore(signal)

    events = store.track_price("BTC/USDT", 94, observed_at=signal.created_at + timedelta(minutes=5))

    assert events[-1].state is SignalState.INVALIDATED
    assert events[-1].invalidated_at is not None
    assert "before entry" in (events[-1].lifecycle_reason or "")
    assert store.track_price("BTC/USDT", 100, observed_at=signal.created_at + timedelta(minutes=6)) == []


def test_price_path_crossing_entry_zone_triggers_instead_of_becoming_missed() -> None:
    store = SignalStore()
    signal = replace(make_waiting_signal(), current_price=98)
    store.restore(signal)

    events = store.track_price("BTC/USDT", 103, observed_at=signal.created_at + timedelta(minutes=5))

    assert events[0].state is SignalState.ENTRY_TRIGGERED
    assert all(event.state is not SignalState.MISSED for event in events)


def test_waiting_setup_expires_and_cannot_trigger_later() -> None:
    store = SignalStore()
    signal = make_waiting_signal()
    store.restore(signal)

    events = store.expire_due(signal.expires_at)

    assert events[-1].state is SignalState.EXPIRED
    assert events[-1].expired_at == signal.expires_at
    assert store.track_price("BTC/USDT", 100, observed_at=signal.expires_at + timedelta(minutes=1)) == []


def test_tp1_then_sl_remains_a_win_state_history() -> None:
    store = SignalStore()
    signal = make_waiting_signal()
    store.restore(signal)
    entry = store.track_price("BTC/USDT", 100, observed_at=signal.created_at + timedelta(minutes=1))[-1]
    assert entry.state is SignalState.ENTRY_TRIGGERED
    tp1 = store.track_price("BTC/USDT", 105, observed_at=signal.created_at + timedelta(minutes=2))[-1]
    stopped = store.track_price("BTC/USDT", 94, observed_at=signal.created_at + timedelta(minutes=3))[-1]

    assert tp1.state is SignalState.TP1_HIT
    assert stopped.state is SignalState.SL_HIT
    assert stopped.tp1_hit_at == tp1.tp1_hit_at


def test_confirmed_signal_is_invalidated_not_stopped_before_entry() -> None:
    store = SignalStore()
    signal = make_signal(state=SignalState.CONFIRMED)
    assert store.should_publish(signal)

    events = store.track_price("BTC/USDT", 94)

    assert events[-1].state is SignalState.INVALIDATED


def test_confirmed_signal_cannot_hit_targets_before_entry() -> None:
    store = SignalStore()
    signal = make_signal(state=SignalState.CONFIRMED)
    assert store.should_publish(signal)

    assert store.track_price("BTC/USDT", 111) == []


def test_confirmed_short_signal_is_invalidated_above_stop_before_entry() -> None:
    store = SignalStore()
    short_trade = TradePlan(99, 101, 100, "retest", "hold", 105, 5, 2.5, "above structure", 95, 90, None, 2)
    signal = replace(make_signal(state=SignalState.CONFIRMED), direction=Direction.SHORT, trade=short_trade)
    assert store.should_publish(signal)

    events = store.track_price("BTC/USDT", 106)

    assert events[-1].state is SignalState.INVALIDATED


def test_active_signal_stop_remains_a_stopped_trade() -> None:
    store = SignalStore()
    assert store.should_publish(make_signal(state=SignalState.ACTIVE))

    events = store.track_price("BTC/USDT", 94)

    assert events[-1].state is SignalState.STOPPED


def test_closed_candle_wick_records_tp1_even_when_close_is_below_target() -> None:
    store = SignalStore()
    started = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
    signal = replace(
        make_signal(state=SignalState.ACTIVE),
        state_changed_at=started,
        activated_at=started,
    )
    assert store.should_publish(signal)

    events = store.track_candles(
        "BTC/USDT",
        [int(started.timestamp() * 1000)],
        [106],
        [99],
        [102],
    )

    assert events[-1].state is SignalState.TP1_HIT
    assert events[-1].current_price == 105


def test_entry_and_stop_in_same_candle_uses_conservative_loss_ordering() -> None:
    store = SignalStore()
    started = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
    signal = replace(make_signal(state=SignalState.CONFIRMED), state_changed_at=started)
    assert store.should_publish(signal)

    events = store.track_candles(
        "BTC/USDT",
        [int(started.timestamp() * 1000)],
        [100],
        [94],
        [96],
    )

    assert [event.state for event in events] == [SignalState.ACTIVE, SignalState.STOPPED]


def test_restart_restore_does_not_replay_already_processed_candle() -> None:
    started = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
    active = replace(
        make_signal(state=SignalState.ACTIVE),
        state_changed_at=started,
        activated_at=started,
    )
    first_store = SignalStore()
    first_store.restore(active)
    timestamp = int(started.timestamp() * 1000)
    events = first_store.track_candles("BTC/USDT", [timestamp], [106], [99], [102])
    tp1 = events[-1]
    assert tp1.state is SignalState.TP1_HIT

    restarted_store = SignalStore()
    restarted_store.restore(tp1)
    assert restarted_store.track_candles("BTC/USDT", [timestamp], [106], [99], [102]) == []


def test_tp1_is_alerted_only_once_while_price_remains_above_target() -> None:
    started = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
    store = SignalStore()
    store.restore(
        replace(
            make_signal(state=SignalState.ACTIVE),
            state_changed_at=started,
            activated_at=started,
        )
    )
    first_timestamp = int(started.timestamp() * 1000)
    first = store.track_candles("BTC/USDT", [first_timestamp], [106], [99], [105.5])
    second = store.track_candles("BTC/USDT", [first_timestamp + 900_000], [107], [103], [106])

    assert [event.state for event in first] == [SignalState.TP1_HIT]
    assert second == []
