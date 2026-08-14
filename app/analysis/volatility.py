from __future__ import annotations

import numpy as np

from app.analysis.indicators import IndicatorSet
from app.models import VolatilityClass


def classify_volatility(indicators: IndicatorSet) -> VolatilityClass:
    natr = indicators.normalized_atr[np.isfinite(indicators.normalized_atr)]
    width = indicators.bb_width[np.isfinite(indicators.bb_width)]
    if natr.size < 30 or width.size < 30:
        return VolatilityClass.NORMAL
    current = float(natr[-1])
    rank = float(np.mean(natr[-100:] <= current))
    expanding = natr[-1] > natr[-5] * 1.15 and width[-1] > width[-5] * 1.15
    if rank >= 0.9:
        return VolatilityClass.HIGH
    if expanding:
        return VolatilityClass.EXPANDING
    if rank <= 0.2:
        return VolatilityClass.LOW
    return VolatilityClass.NORMAL
