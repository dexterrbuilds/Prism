from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.models import Direction, MarketRegime, Signal, SignalGrade, SignalState, TradePlan
from app.signals.lifecycle import SignalStore, transition


def make_signal(*, score: int = 85, state: SignalState = SignalState.ACTIVE, entry: float = 100) -> Signal:
    trade = TradePlan(99, 101, entry, "retest", "hold", 95, 5, 2.5, "below structure", 105, 110, None, 2)
    return Signal("id", "BTC/USDT", "BREAKOUT_RETEST", Direction.LONG, MarketRegime.BULLISH_TREND, score, SignalGrade.VALID, state, trade, ("evidence",), datetime.now(UTC))


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


def test_signal_state_transitions_are_guarded() -> None:
    detected = make_signal(state=SignalState.DETECTED)
    watching = transition(detected, SignalState.WATCHING)
    confirmed = transition(watching, SignalState.CONFIRMED)
    active = transition(confirmed, SignalState.ACTIVE)
    assert transition(active, SignalState.TP1_HIT).state is SignalState.TP1_HIT
    with pytest.raises(ValueError):
        transition(detected, SignalState.TP2_HIT)


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
