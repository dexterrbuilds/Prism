from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.models import SignalState
from app.signals.lifecycle import transition
from app.signals.outcomes import OutcomeLedger
from tests.test_lifecycle import make_signal


def test_tp1_is_persisted_as_a_win_even_if_runner_later_stops(tmp_path) -> None:
    ledger = OutcomeLedger(str(tmp_path / "signals.db"))
    started = datetime.now(UTC)
    active = replace(
        make_signal(state=SignalState.ACTIVE),
        id="winner",
        created_at=started,
        state_changed_at=started,
        activated_at=started,
        current_price=100,
    )
    assert ledger.record_signal(active)
    tp1 = transition(active, SignalState.TP1_HIT, current_price=105, changed_at=started + timedelta(hours=2))
    assert ledger.record_event(tp1)

    stats = ledger.stats(now=started + timedelta(hours=2, minutes=1))
    assert stats.wins == 1
    assert stats.losses == 0
    assert stats.win_rate == 100
    assert stats.average_hold_hours == pytest.approx(2)
    assert ledger.load_open_signals()[0].state is SignalState.TP1_HIT

    stopped_runner = transition(tp1, SignalState.STOPPED, current_price=95, changed_at=started + timedelta(hours=3))
    assert ledger.record_event(stopped_runner)
    stats = ledger.stats(now=started + timedelta(hours=3, minutes=1))
    assert stats.wins == 1
    assert stats.losses == 0
    ledger.close()


def test_stopped_before_tp1_is_a_loss_and_history_survives_restart(tmp_path) -> None:
    path = str(tmp_path / "signals.db")
    ledger = OutcomeLedger(path)
    started = datetime.now(UTC)
    active = replace(
        make_signal(state=SignalState.ACTIVE),
        id="loser",
        created_at=started,
        state_changed_at=started,
        activated_at=started,
        current_price=100,
    )
    assert ledger.record_signal(active)
    stopped = transition(active, SignalState.STOPPED, current_price=95, changed_at=started + timedelta(hours=1))
    assert ledger.record_event(stopped)
    ledger.close()

    reopened = OutcomeLedger(path)
    stats = reopened.stats(now=started + timedelta(hours=1, minutes=1))
    assert stats.wins == 0
    assert stats.losses == 1
    assert stats.win_rate == 0
    assert stats.resolved == 1
    reopened.close()


def test_direct_tp2_hit_also_counts_as_a_tp1_win(tmp_path) -> None:
    ledger = OutcomeLedger(str(tmp_path / "signals.db"))
    started = datetime.now(UTC)
    active = replace(
        make_signal(state=SignalState.ACTIVE),
        id="tp2",
        created_at=started,
        state_changed_at=started,
        activated_at=started,
    )
    assert ledger.record_signal(active)
    tp2 = transition(active, SignalState.TP2_HIT, current_price=110, changed_at=started + timedelta(hours=4))
    assert ledger.record_event(tp2)

    stats = ledger.stats(now=started + timedelta(hours=4, minutes=1))
    assert stats.wins == 1
    assert stats.tp2_hits == 1
    assert stats.win_rate == 100
    ledger.close()


def test_duplicate_and_stale_events_cannot_duplicate_or_regress_outcome(tmp_path) -> None:
    ledger = OutcomeLedger(str(tmp_path / "signals.db"))
    started = datetime.now(UTC)
    active = replace(
        make_signal(state=SignalState.ACTIVE),
        id="idempotent",
        created_at=started,
        state_changed_at=started,
        activated_at=started,
    )
    assert ledger.record_signal(active)
    assert not ledger.record_signal(active)
    tp1 = transition(active, SignalState.TP1_HIT, current_price=105, changed_at=started + timedelta(hours=1))
    tp2 = transition(tp1, SignalState.TP2_HIT, current_price=110, changed_at=started + timedelta(hours=2))
    assert ledger.record_event(tp1)
    assert not ledger.record_event(tp1)
    assert ledger.record_event(tp2)
    assert not ledger.record_event(tp1)

    stats = ledger.stats(now=started + timedelta(hours=2, minutes=1))
    assert stats.signals == 1
    assert stats.wins == 1
    assert stats.tp2_hits == 1
    assert stats.tp1_runners == 0
    ledger.close()


def test_confirmed_never_entered_signal_is_excluded_from_win_rate(tmp_path) -> None:
    ledger = OutcomeLedger(str(tmp_path / "signals.db"))
    started = datetime.now(UTC)
    confirmed = replace(
        make_signal(state=SignalState.CONFIRMED),
        id="never-entered",
        created_at=started,
        state_changed_at=started,
        activated_at=None,
    )
    assert ledger.record_signal(confirmed)

    stats = ledger.stats(now=started + timedelta(hours=1))
    assert stats.signals == 1
    assert stats.activated == 0
    assert stats.resolved == 0
    assert stats.win_rate is None
    assert stats.open_signals == 1
    ledger.close()
