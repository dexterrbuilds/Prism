from __future__ import annotations

from dataclasses import replace

import numpy as np

from app.models import (
    ConfluenceEvidence,
    Direction,
    EntryDecision,
    SetupCandidate,
    SignalMode,
    StructureBias,
    StructureEvent,
    StructureState,
    TradePlan,
)
from app.signals.entry_plan import calculate_entry_plan
from app.signals.entry_quality import grade_entry_quality, score_entry_quality, two_gate_result
from tests.helpers import analysis_context, candles


def _candidate(mode: SignalMode = SignalMode.INTRADAY) -> SetupCandidate:
    return SetupCandidate(
        symbol="BTC/USDT",
        strategy="BREAKOUT_RETEST",
        direction=Direction.LONG,
        timeframe="1h" if mode is SignalMode.INTRADAY else "15m",
        detected_at_ms=1,
        ideal_entry_low=99.5,
        ideal_entry_high=100.5,
        trigger="closed retest",
        invalidation_level=97.0,
        quality=0.9,
        evidence=ConfluenceEvidence(),
        confirmed=True,
        mode=mode,
    )


def _plan() -> TradePlan:
    return TradePlan(99.5, 100.5, 100.0, "breakout retest", "closed retest", 96.7, 3.3, 1.65, "below structure", 103.3, 106.6, None, 2.0, invalidation_level=97.0)


def _context(*, retest: bool) -> object:
    one_hour = candles(np.linspace(95, 100, 250), "1h")
    primary_structure = StructureState(StructureBias.BULLISH, (), (), 105, 97, 105, 97)
    context = analysis_context(one_hour, primary_structure, momentum_direction=Direction.LONG, volume_direction=Direction.LONG)
    lower_close = np.full(250, 102.0)
    lower_close[-1] = 100.2 if retest else 102.0
    lower_open = lower_close.copy()
    lower_open[-1] = 99.8 if retest else 101.8
    lower_low = lower_close - 0.2
    lower_high = lower_close + 0.2
    if retest:
        lower_low[-2:] = (99.7, 99.8)
    lower_candles = candles(lower_close, "15m", open_=lower_open, low=lower_low, high=lower_high)
    event = StructureEvent("CHOCH", Direction.LONG, 249, 100.0)
    lower_structure = StructureState(StructureBias.BULLISH, (), (event,), 102, 99, 102, 99)
    lower_analysis = replace(context.timeframes["15m"], structure=lower_structure)
    snapshot = replace(context.snapshot, series={**context.snapshot.series, "15m": lower_candles})
    return replace(context, snapshot=snapshot, timeframes={**context.timeframes, "15m": lower_analysis})


def test_entry_quality_thresholds_and_two_independent_gates() -> None:
    assert grade_entry_quality(64) is EntryDecision.REJECT
    assert grade_entry_quality(70) is EntryDecision.WAIT
    assert grade_entry_quality(80) is EntryDecision.VALID
    assert grade_entry_quality(90) is EntryDecision.HIGH_QUALITY
    quality = score_entry_quality(_candidate(), _context(retest=True), _plan(), current_price=100.2)  # type: ignore[arg-type]
    assert quality.total <= 100
    assert two_gate_result(79, quality).actionable is False
    assert two_gate_result(80, quality).actionable is True


def test_breakout_waits_without_retest_and_confirms_after_reclaim() -> None:
    waiting = score_entry_quality(_candidate(), _context(retest=False), _plan(), current_price=102.0)  # type: ignore[arg-type]
    ready = score_entry_quality(_candidate(), _context(retest=True), _plan(), current_price=100.2)  # type: ignore[arg-type]
    assert waiting.retest_completed is False
    assert waiting.total < 75
    assert ready.retest_completed is True
    assert ready.lower_timeframe_confirmed is True
    assert ready.total >= 75


def test_excessive_atr_chase_is_a_hard_reject() -> None:
    quality = score_entry_quality(_candidate(), _context(retest=False), _plan(), current_price=104.0, max_chase_atr=0.75)  # type: ignore[arg-type]
    assert "ENTRY_TOO_LATE" in quality.hard_reasons
    assert quality.decision is EntryDecision.REJECT


def test_breakout_entry_plan_anchors_to_broken_level_not_market_price() -> None:
    broad = replace(_candidate(), ideal_entry_low=100.0, ideal_entry_high=104.0)
    plan = calculate_entry_plan(broad, _context(retest=False))  # type: ignore[arg-type]
    assert plan.preferred_entry == 100.0
    assert plan.zone_high < 101.0
    assert "broken structure retest level" in plan.source_levels
