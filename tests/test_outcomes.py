from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.models import SignalState
from app.signals.lifecycle import transition
from app.signals.outcomes import OutcomeLedger
from tests.test_lifecycle import make_signal, make_waiting_signal


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


def test_setup_lifecycle_survives_restart_with_expiry_and_trigger_data(tmp_path) -> None:
    path = str(tmp_path / "signals.db")
    ledger = OutcomeLedger(path)
    waiting = replace(make_waiting_signal(), id="restartable")
    assert ledger.record_signal(waiting)
    triggered = transition(
        waiting,
        SignalState.ENTRY_TRIGGERED,
        current_price=100,
        trigger_price=100,
        changed_at=waiting.created_at + timedelta(minutes=30),
    )
    assert ledger.record_event(triggered)
    assert not ledger.record_event(triggered)
    ledger.close()

    reopened = OutcomeLedger(path)
    restored = reopened.load_open_signals()
    assert len(restored) == 1
    assert restored[0].state is SignalState.ENTRY_TRIGGERED
    assert restored[0].expires_at == waiting.expires_at
    assert restored[0].entry_trigger_price == 100
    assert restored[0].validity_minutes == 360
    reopened.close()


@pytest.mark.parametrize(
    ("state", "timestamp_field"),
    [
        (SignalState.MISSED, "missed_at"),
        (SignalState.INVALIDATED, "invalidated_at"),
        (SignalState.EXPIRED, "expired_at"),
    ],
)
def test_pre_entry_terminal_setup_states_never_count_as_losses(tmp_path, state: SignalState, timestamp_field: str) -> None:
    ledger = OutcomeLedger(str(tmp_path / f"{state.value}.db"))
    waiting = replace(make_waiting_signal(), id=state.value)
    assert ledger.record_signal(waiting)
    terminal = transition(
        waiting,
        state,
        current_price=103,
        changed_at=waiting.created_at + timedelta(hours=1),
        reason=f"{state.value} reason",
    )
    assert getattr(terminal, timestamp_field) is not None
    assert ledger.record_event(terminal)
    assert not ledger.record_event(terminal)

    stats = ledger.stats(now=waiting.created_at + timedelta(hours=2))
    assert stats.activated == 0
    assert stats.wins == 0
    assert stats.losses == 0
    assert stats.resolved == 0
    assert stats.invalidated == 1
    ledger.close()


def test_new_setup_tp1_win_survives_later_sl_hit(tmp_path) -> None:
    ledger = OutcomeLedger(str(tmp_path / "new-state-win.db"))
    waiting = replace(make_waiting_signal(), id="new-state-winner")
    assert ledger.record_signal(waiting)
    active = transition(
        waiting,
        SignalState.ENTRY_TRIGGERED,
        current_price=100,
        trigger_price=100,
        changed_at=waiting.created_at + timedelta(minutes=10),
    )
    tp1 = transition(
        active,
        SignalState.TP1_HIT,
        current_price=105,
        changed_at=waiting.created_at + timedelta(hours=1),
    )
    stopped = transition(
        tp1,
        SignalState.SL_HIT,
        current_price=95,
        changed_at=waiting.created_at + timedelta(hours=2),
    )
    assert ledger.record_event(active)
    assert ledger.record_event(tp1)
    assert ledger.record_event(stopped)

    stats = ledger.stats(now=waiting.created_at + timedelta(hours=3))
    assert stats.wins == 1
    assert stats.losses == 0
    assert stats.win_rate == 100
    ledger.close()


def test_new_setup_sl_before_tp1_records_loss(tmp_path) -> None:
    ledger = OutcomeLedger(str(tmp_path / "new-state-loss.db"))
    waiting = replace(make_waiting_signal(), id="new-state-loser")
    assert ledger.record_signal(waiting)
    active = transition(
        waiting,
        SignalState.ENTRY_TRIGGERED,
        current_price=100,
        trigger_price=100,
        changed_at=waiting.created_at + timedelta(minutes=10),
    )
    stopped = transition(
        active,
        SignalState.SL_HIT,
        current_price=95,
        changed_at=waiting.created_at + timedelta(hours=1),
    )
    assert ledger.record_event(active)
    assert ledger.record_event(stopped)

    stats = ledger.stats(now=waiting.created_at + timedelta(hours=2))
    assert stats.wins == 0
    assert stats.losses == 1
    assert stats.win_rate == 0
    ledger.close()
