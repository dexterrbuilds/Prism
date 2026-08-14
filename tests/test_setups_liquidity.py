from __future__ import annotations

import numpy as np

from app.analysis.liquidity import detect_liquidity_events
from app.models import Direction, StructureBias, StructureState, SwingKind, SwingLabel, SwingPoint
from app.strategies.breakout import BreakoutSetupDetector
from tests.helpers import analysis_context, candles


def test_breakout_requires_close_and_volume_confirmation() -> None:
    close = np.linspace(90, 99, 250)
    close[-2:] = [99, 101]
    series = candles(close)
    structure = StructureState(StructureBias.BULLISH, (), (), 100, 90, 100, 90)
    context = analysis_context(series, structure, momentum_direction=Direction.LONG, volume_direction=Direction.LONG, relative_volume=1.5)
    strategies = {candidate.strategy for candidate in BreakoutSetupDetector().detect(context)}
    assert "BREAKOUT" in strategies


def test_breakout_rejected_without_relative_volume() -> None:
    close = np.linspace(90, 99, 250)
    close[-2:] = [99, 101]
    series = candles(close)
    structure = StructureState(StructureBias.BULLISH, (), (), 100, 90, 100, 90)
    context = analysis_context(series, structure, momentum_direction=Direction.LONG, relative_volume=0.8)
    strategies = {candidate.strategy for candidate in BreakoutSetupDetector().detect(context)}
    assert "BREAKOUT" not in strategies


def test_liquidity_sweep_and_reclaim() -> None:
    close = np.full(30, 102.0)
    open_ = close.copy()
    high = close + 1
    low = close - 1
    open_[-1], low[-1], high[-1], close[-1] = 99.0, 97.0, 103.0, 102.0
    pivot = SwingPoint(15, 15_000, 100, SwingKind.LOW, SwingLabel.LOW, 17)
    structure = StructureState(StructureBias.RANGE, (pivot,), (), None, 100, None, 100)
    events = detect_liquidity_events(candles(close, high=high, low=low, open_=open_), structure, atr=2)
    assert any(event.direction is Direction.LONG and "sweep" in event.name for event in events)


def test_failed_breakout_detected_as_bull_trap() -> None:
    close = np.full(30, 99.0)
    close[-3:] = [99, 102, 98]
    open_ = close.copy()
    high, low = close + 1, close - 1
    pivot = SwingPoint(15, 15_000, 100, SwingKind.HIGH, SwingLabel.HIGH, 17)
    structure = StructureState(StructureBias.RANGE, (pivot,), (), 100, None, 100, None)
    events = detect_liquidity_events(candles(close, high=high, low=low, open_=open_), structure, atr=2)
    assert any(event.direction is Direction.SHORT and "failed breakout" in event.name for event in events)
