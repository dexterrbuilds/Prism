from __future__ import annotations

from dataclasses import replace

from app.telegram.formatter import format_lifecycle, format_signal, format_watch
from tests.test_lifecycle import make_signal


def test_signal_format_is_clean_compact_and_chart_caption_safe() -> None:
    signal = replace(
        make_signal(score=88),
        supporting_strategies=("BOS_CONTINUATION", "EMA_PULLBACK"),
        evidence=tuple(f"Observed evidence item {index} with deterministic confirmation" for index in range(8)),
    )
    message = format_signal(signal)
    assert "🟢 *BTC/USDT · LONG*" in message
    assert "🎯 *Trade Plan*" in message
    assert "🔎 *Why It Qualifies*" in message
    assert "🛑 *Invalidation*" in message
    assert "Supports: Bos Continuation · Ema Pullback" in message
    assert message.count("•") == 4
    assert "🧮 *$5,000 Margin Example*" in message
    assert "2× · $10,000 notional" in message
    assert "5× · $25,000 notional" in message
    assert len(message) <= 1024


def test_watch_and_lifecycle_messages_are_compact() -> None:
    signal = make_signal()
    assert "Confirmation Needed" in format_watch(signal)
    assert "Trade Plan" not in format_watch(signal)
    assert "🚀" in format_lifecycle(signal)
