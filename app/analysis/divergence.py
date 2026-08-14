from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.analysis.indicators import IndicatorSet
from app.models import Direction, StructureState, SwingKind


@dataclass(frozen=True, slots=True)
class DivergenceDetection:
    name: str
    direction: Direction
    indicator: str
    first_index: int
    second_index: int
    quality: float


def detect_divergences(structure: StructureState, indicators: IndicatorSet) -> tuple[DivergenceDetection, ...]:
    detections: list[DivergenceDetection] = []
    sources = {"RSI": indicators.rsi, "MACD histogram": indicators.macd_hist, "OBV": indicators.obv}
    for kind, direction in ((SwingKind.LOW, Direction.LONG), (SwingKind.HIGH, Direction.SHORT)):
        pivots = [s for s in structure.swings if s.kind is kind]
        if len(pivots) < 2:
            continue
        first, second = pivots[-2:]
        if second.index - first.index < 3:
            continue
        for indicator_name, values in sources.items():
            a, b = float(values[first.index]), float(values[second.index])
            if not np.isfinite(a) or not np.isfinite(b):
                continue
            scale = max(abs(a), abs(b), 1e-9)
            indicator_delta = (b - a) / scale
            price_delta = (second.price - first.price) / max(abs(first.price), 1e-9)
            regular = (direction is Direction.LONG and price_delta < -0.001 and indicator_delta > 0.01) or (
                direction is Direction.SHORT and price_delta > 0.001 and indicator_delta < -0.01
            )
            hidden = (direction is Direction.LONG and price_delta > 0.001 and indicator_delta < -0.01) or (
                direction is Direction.SHORT and price_delta < -0.001 and indicator_delta > 0.01
            )
            if regular or hidden:
                quality = min(1.0, (abs(price_delta) * 20 + abs(indicator_delta) * 2))
                detections.append(
                    DivergenceDetection("regular" if regular else "hidden", direction, indicator_name, first.index, second.index, quality)
                )
    return tuple(detections)
