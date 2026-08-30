from __future__ import annotations

from dataclasses import replace

import numpy as np

from app.models import Direction, SignalState, StructureBias, StructureEvent, StructureState
from app.strategies.counter import evaluate_counter_setup
from tests.helpers import analysis_context, candles
from tests.test_lifecycle import make_waiting_signal


def test_invalidation_does_not_automatically_create_counter_trade() -> None:
    original = replace(make_waiting_signal(), state=SignalState.INVALIDATED)
    one_hour = candles(np.linspace(95, 100, 250), "1h")
    structure = StructureState(StructureBias.BULLISH, (), (), 105, 95, 105, 95)
    context = analysis_context(one_hour, structure)
    assert evaluate_counter_setup(original, context) is None


def test_failed_bullish_setup_requires_bearish_retest_and_structure() -> None:
    original = replace(make_waiting_signal(), state=SignalState.INVALIDATED)
    one_hour = candles(np.linspace(95, 100, 250), "1h")
    structure = StructureState(StructureBias.BEARISH, (), (), 105, 95, 105, 95)
    context = analysis_context(
        one_hour,
        structure,
        momentum_direction=Direction.SHORT,
        volume_direction=Direction.SHORT,
    )
    close = np.full(250, 98.5)
    close[-1] = 98.8
    lower = candles(close, "15m", high=np.r_[np.full(249, 99.0), 99.2])
    event = StructureEvent("CHOCH", Direction.SHORT, 249, 99.0)
    lower_structure = StructureState(StructureBias.BEARISH, (), (event,), 101, 95, 101, 95)
    updated = replace(
        context,
        snapshot=replace(context.snapshot, series={**context.snapshot.series, "15m": lower}),
        timeframes={**context.timeframes, "15m": replace(context.timeframes["15m"], structure=lower_structure)},
    )
    counter = evaluate_counter_setup(original, updated)
    assert counter is not None
    assert counter.strategy == "FAILED_BREAKOUT_SHORT"
    assert counter.direction is Direction.SHORT
    assert counter.metadata["counter_of"] == original.id
