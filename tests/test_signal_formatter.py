from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.models import SignalState
from app.signals.outcomes import PerformanceStats
from app.telegram.formatter import format_lifecycle, format_signal, format_stats, format_watch
from tests.test_lifecycle import make_signal


def test_signal_format_is_clean_compact_and_chart_caption_safe() -> None:
    signal = replace(
        make_signal(score=88),
        supporting_strategies=("BOS_CONTINUATION", "EMA_PULLBACK"),
        evidence=tuple(f"Observed evidence item {index} with deterministic confirmation" for index in range(8)),
        current_price=102.5,
    )
    message = format_signal(signal)
    assert "🟢 *BTC/USDT · LONG*" in message
    assert "🎯 *Trade Plan*" in message
    assert "🔎 *Why It Qualifies*" in message
    assert "🛑 *Invalidation*" in message
    assert "Supports: Bos Continuation · Ema Pullback" in message
    assert message.count("•") == 4
    assert "🧮 *$5,000 Margin Example*" in message
    assert "💹 *Current Price*" in message
    assert "$102.5000 · latest closed 15M candle" in message
    assert "⚡ *Entry Trigger*" in message
    assert "hold" in message
    assert "2× · $10,000 notional" in message
    assert "5× · $25,000 notional" in message
    assert len(message) <= 1024


def test_watch_and_lifecycle_messages_are_compact() -> None:
    signal = make_signal()
    assert "Confirmation Needed" in format_watch(signal)
    assert "Trade Plan" not in format_watch(signal)
    assert "🚀" in format_lifecycle(signal)


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
