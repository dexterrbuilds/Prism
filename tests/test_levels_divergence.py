from __future__ import annotations

import numpy as np

from app.analysis.divergence import detect_divergences
from app.analysis.support_resistance import cluster_levels
from app.models import Direction, StructureBias, StructureState, SwingKind, SwingLabel, SwingPoint
from tests.helpers import indicators


def swing(index: int, price: float, kind: SwingKind, label: SwingLabel) -> SwingPoint:
    return SwingPoint(index, index * 1000, price, kind, label, index + 2)


def test_support_resistance_clustering_uses_atr_tolerance_and_reactions() -> None:
    swings = (
        swing(10, 99.8, SwingKind.LOW, SwingLabel.LOW),
        swing(20, 100.2, SwingKind.LOW, SwingLabel.HL),
        swing(30, 110, SwingKind.HIGH, SwingLabel.HIGH),
    )
    structure = StructureState(StructureBias.RANGE, swings, (), 110, 100.2, 110, 99.8, previous_day_low=100.1)
    zones = cluster_levels(structure, indicators(50), current_price=105, atr=2, tolerance_atr=0.5)
    cluster = next(zone for zone in zones if zone.low <= 100 <= zone.high)
    assert cluster.reactions >= 3
    assert cluster.score >= 5
    assert "previous_day_low" in cluster.sources


def test_regular_bullish_rsi_divergence_uses_confirmed_pivots() -> None:
    swings = (
        swing(20, 100, SwingKind.LOW, SwingLabel.LOW),
        swing(40, 95, SwingKind.LOW, SwingLabel.LL),
    )
    structure = StructureState(StructureBias.BEARISH, swings, (), None, 95, None, None)
    rsi = np.full(60, 50.0)
    rsi[20], rsi[40] = 30, 40
    detections = detect_divergences(structure, indicators(60, rsi=rsi))
    assert any(d.name == "regular" and d.direction is Direction.LONG and d.indicator == "RSI" for d in detections)
