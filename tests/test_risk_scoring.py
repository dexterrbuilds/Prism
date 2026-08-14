from __future__ import annotations

import pytest

from app.models import ConfluenceEvidence, Direction, SetupCandidate, SignalGrade, StructureBias, StructureState
from app.signals.risk import build_trade_plan, is_entry_too_late, two_r_target
from app.signals.scoring import CATEGORY_CAPS, grade_score, score_candidate
from tests.helpers import analysis_context, candles


def setup(direction: Direction = Direction.LONG) -> SetupCandidate:
    return SetupCandidate(
        "BTC/USDT", "BREAKOUT_RETEST", direction, "1h", 1, 99, 101, "close and hold", 96 if direction is Direction.LONG else 104,
        0.9,
        ConfluenceEvidence(
            trend=["trend"], structure=["BOS"], location=["retest"], momentum=["momentum"],
            volume=["volume"], pattern=["pattern"], candlestick=["engulfing"], volatility=["expanding"], higher_timeframe=["4H aligned"],
        ),
        True,
    )


def test_atr_structure_stop_and_two_r() -> None:
    plan = build_trade_plan(setup(), current_price=100, atr=2, zones=())
    assert plan.stop_loss == 95.7
    assert plan.risk_per_unit == pytest.approx(4.3)
    assert plan.tp2 == pytest.approx(108.6)
    assert plan.stop_distance_atr == pytest.approx(2.15)
    assert two_r_target(100, 104, Direction.SHORT) == 92


def test_entry_too_late_is_directional_and_atr_relative() -> None:
    candidate = setup()
    assert is_entry_too_late(103, candidate, atr=2, max_chase_atr=0.75)
    assert not is_entry_too_late(101.5, candidate, atr=2, max_chase_atr=0.75)
    assert not is_entry_too_late(97, candidate, atr=2, max_chase_atr=0.75)


def test_score_calculation_respects_category_caps() -> None:
    series = candles([100.0] * 250)
    structure = StructureState(StructureBias.BULLISH, (), (), 110, 90, 110, 90)
    context = analysis_context(series, structure, momentum_direction=Direction.LONG, volume_direction=Direction.LONG)
    result = score_candidate(setup(), context)
    assert result.total <= 100
    assert sum(result.categories.values()) == result.total
    assert all(result.categories[name] <= cap for name, cap in CATEGORY_CAPS.items())
    assert grade_score(69) is SignalGrade.IGNORE
    assert grade_score(70) is SignalGrade.WATCH
    assert grade_score(80) is SignalGrade.VALID
    assert grade_score(90) is SignalGrade.EXCEPTIONAL
