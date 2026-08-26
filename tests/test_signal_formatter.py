from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.models import SignalState
from app.signals.outcomes import PerformanceStats
from app.telegram.formatter import format_lifecycle, format_signal, format_stats, format_watch
from tests.test_lifecycle import make_signal, make_waiting_signal


def test_signal_format_is_clean_compact_and_chart_caption_safe() -> None:
    signal = replace(
        make_signal(score=88),
        supporting_strategies=("BOS_CONTINUATION", "EMA_PULLBACK"),
        evidence=tuple(f"Observed evidence item {index} with deterministic confirmation" for index in range(8)),
        current_price=102.5,
    )
    message = format_signal(signal)
    assert "🚨 *BTC/USDT · LONG*" in message
    assert "SETUP DETECTED · LIVE SETUP" in message
    assert "🎯 *ENTRY*" in message
    assert "🔎 *Confluence*" in message
    assert "🛡 *INVALIDATION*" in message
    assert "Supports: Bos Continuation · Ema Pullback" in message
    assert message.count("•") == 4
    assert "🧮 *$5K Margin Simulation*" in message
    assert "📍 *Current Price*" in message
    assert "$102.5000" in message
    assert "Trigger: hold" in message
    assert "hold" in message
    assert "2× $10,000" in message
    assert "5× $25,000" in message
    assert len(message) <= 1024


def test_watch_and_lifecycle_messages_are_compact() -> None:
    signal = make_signal()
    assert "Confirmation Needed" in format_watch(signal)
    assert "Trade Plan" not in format_watch(signal)
    assert "🟢 *ENTRY TRIGGERED*" in format_lifecycle(signal)


def test_live_setup_displays_configured_validity_and_exact_expiry() -> None:
    signal = make_waiting_signal()
    message = format_signal(signal)
    assert "SETUP VALID FOR: 6 HOURS" in message
    assert "EXPIRES: 2030-08-26 18:00 UTC" in message
    assert "Trade 15M · Analysis 1H" in message
    assert "VALID WHILE" in message


def test_entry_and_terminal_setup_alerts_are_distinct() -> None:
    waiting = make_waiting_signal()
    triggered = replace(
        waiting,
        state=SignalState.ENTRY_TRIGGERED,
        current_price=100,
        activated_at=waiting.created_at + timedelta(minutes=15),
        state_changed_at=waiting.created_at + timedelta(minutes=15),
        entry_trigger_price=100,
    )
    missed = replace(
        waiting,
        state=SignalState.MISSED,
        current_price=103,
        missed_at=waiting.created_at + timedelta(minutes=15),
        state_changed_at=waiting.created_at + timedelta(minutes=15),
        lifecycle_reason="Price moved beyond the actionable entry area without triggering the setup.",
    )
    invalidated = replace(
        waiting,
        state=SignalState.INVALIDATED,
        current_price=94,
        invalidated_at=waiting.created_at + timedelta(minutes=15),
        state_changed_at=waiting.created_at + timedelta(minutes=15),
        lifecycle_reason="Price broke below the setup invalidation level before entry.",
    )
    expired = replace(
        waiting,
        state=SignalState.EXPIRED,
        expired_at=waiting.expires_at,
        state_changed_at=waiting.expires_at,
        lifecycle_reason="Setup validity window elapsed before the entry zone was triggered.",
    )

    assert "Status: *ACTIVE*" in format_lifecycle(triggered)
    assert "Actual Trigger" in format_lifecycle(triggered)
    assert "SETUP MISSED" in format_lifecycle(missed)
    assert "No trade was activated" in format_lifecycle(missed)
    assert "SETUP INVALIDATED" in format_lifecycle(invalidated)
    assert "Invalidation Level" in format_lifecycle(invalidated)
    assert "SETUP EXPIRED" in format_lifecycle(expired)
    assert "Duration*  6 HOURS" in format_lifecycle(expired)


def test_tp1_lifecycle_alert_records_win_and_next_target() -> None:
    now = datetime.now(UTC)
    signal = replace(
        make_signal(state=SignalState.TP1_HIT),
        current_price=105,
        activated_at=now,
        tp1_hit_at=now,
        state_changed_at=now,
    )
    message = format_lifecycle(signal)
    assert "TP1 HIT · WIN RECORDED" in message
    assert "Next objective" in message
    assert "TP1 is counted as a win" in message


def test_stats_formatter_states_win_rule_and_sample_size() -> None:
    now = datetime.now(UTC)
    stats = PerformanceStats(now, None, 4, 3, 2, 1, 0, 1, 1, 1, 6.5)
    message = format_stats(stats)
    assert "TP1 counts as a win" in message
    assert "66.7%" in message
    assert "2W / 1L" in message
    assert "Small sample" in message
