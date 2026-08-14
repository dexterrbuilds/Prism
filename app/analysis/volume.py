from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.analysis.indicators import IndicatorSet
from app.models import Direction


@dataclass(frozen=True, slots=True)
class VolumeState:
    direction: Direction | None
    relative_volume: float
    breakout_confirmed: bool
    climax: bool
    evidence: tuple[str, ...]


def evaluate_volume(indicators: IndicatorSet) -> VolumeState:
    rv = float(indicators.relative_volume[-1])
    evidence: list[str] = []
    obv_delta = float(indicators.obv[-1] - indicators.obv[-5])
    ad_delta = float(indicators.ad[-1] - indicators.ad[-5])
    direction = None
    if obv_delta > 0 and ad_delta > 0:
        direction = Direction.LONG
        evidence.append("OBV and accumulation/distribution confirming")
    elif obv_delta < 0 and ad_delta < 0:
        direction = Direction.SHORT
        evidence.append("OBV and accumulation/distribution weakening")
    if np.isfinite(rv) and rv >= 1.35:
        evidence.append("Relative volume expansion")
    climax = bool(np.isfinite(rv) and rv >= 2.8)
    return VolumeState(direction, rv, bool(np.isfinite(rv) and rv >= 1.2), climax, tuple(evidence))
