from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from app.analysis.indicators import calculate_indicators
from app.models import Direction, StructureBias, StructureState
from app.strategies.scalp import ScalpSetupDetector
from tests.helpers import analysis_context, candles, indicators


def test_vwap_is_volume_weighted_and_resets_by_utc_day() -> None:
    series = candles(
        [10.0, 12.0, 20.0],
        "5m",
        high=np.array([10.0, 12.0, 20.0]),
        low=np.array([10.0, 12.0, 20.0]),
        open_=np.array([10.0, 12.0, 20.0]),
        volume=np.array([1.0, 3.0, 2.0]),
    )
    values = calculate_indicators(series).vwap
    assert values[0] == 10.0
    assert values[1] == 11.5
    assert values[2] == pytest.approx(14.3333333333)


def test_scalp_detector_routes_vwap_reclaim_on_5m() -> None:
    one_hour = candles(np.linspace(95, 100, 250), "1h")
    structure = StructureState(StructureBias.BULLISH, (), (), 105, 94, 105, 94)
    context = analysis_context(one_hour, structure, momentum_direction=Direction.LONG, volume_direction=Direction.LONG)
    close = np.full(250, 99.8)
    close[-1] = 100.2
    five = candles(close, "5m", open_=np.r_[close[:-1], 99.9])
    five_analysis = replace(context.timeframes["15m"], indicators=indicators(250, price=100.0))
    updated = replace(
        context,
        snapshot=replace(context.snapshot, series={**context.snapshot.series, "5m": five}),
        timeframes={**context.timeframes, "5m": five_analysis},
    )
    names = {item.strategy for item in ScalpSetupDetector().detect(updated)}
    assert "SCALP_VWAP_RECLAIM" in names
