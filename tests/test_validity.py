from __future__ import annotations

from dataclasses import replace

import pytest

from app.models import ConfluenceEvidence, Direction, SetupCandidate
from app.signals.risk import estimate_hold_time
from app.signals.validity import derive_setup_validity_minutes, timeframe_minutes
from tests.test_lifecycle import make_signal


def _candidate(strategy: str, *, timeframe: str = "1h", **metadata: float) -> SetupCandidate:
    return SetupCandidate(
        symbol="BTC/USDT",
        strategy=strategy,
        direction=Direction.LONG,
        timeframe=timeframe,
        detected_at_ms=1,
        ideal_entry_low=99,
        ideal_entry_high=101,
        trigger="confirmation",
        invalidation_level=95,
        quality=0.8,
        evidence=ConfluenceEvidence(),
        confirmed=True,
        metadata=metadata,
    )


def test_setup_validity_depends_on_strategy_aware_projected_horizon() -> None:
    trade = make_signal().trade
    breakout_low, breakout_high = estimate_hold_time("BREAKOUT", 2)
    reversal_low, reversal_high = estimate_hold_time("CHOCH_REVERSAL", 2)
    breakout_trade = replace(
        trade,
        estimated_hold_hours_low=breakout_low,
        estimated_hold_hours_high=breakout_high,
    )
    reversal_trade = replace(
        trade,
        estimated_hold_hours_low=reversal_low,
        estimated_hold_hours_high=reversal_high,
    )

    breakout_validity = derive_setup_validity_minutes(_candidate("BREAKOUT"), breakout_trade)
    reversal_validity = derive_setup_validity_minutes(_candidate("CHOCH_REVERSAL"), reversal_trade)

    assert breakout_validity == breakout_low * 60
    assert reversal_validity == reversal_low * 60
    assert reversal_validity > breakout_validity


def test_detector_override_is_expressed_in_analysis_bars_not_fixed_hours() -> None:
    candidate = _candidate("BREAKOUT_RETEST", timeframe="4h", validity_bars=3)
    assert derive_setup_validity_minutes(candidate, make_signal().trade) == 3 * timeframe_minutes("4h")


def test_validity_fallback_uses_target_geometry_and_timeframe() -> None:
    trade = replace(
        make_signal().trade,
        estimated_hold_hours_low=None,
        estimated_hold_hours_high=None,
    )
    assert derive_setup_validity_minutes(_candidate("CUSTOM", timeframe="15m"), trade) == 30


def test_invalid_timeframe_or_bar_override_is_rejected() -> None:
    with pytest.raises(ValueError, match="timeframe"):
        derive_setup_validity_minutes(_candidate("CUSTOM", timeframe="bad"), make_signal().trade)
    with pytest.raises(ValueError, match="positive"):
        derive_setup_validity_minutes(_candidate("CUSTOM", validity_bars=0), make_signal().trade)
