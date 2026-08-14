from __future__ import annotations

import numpy as np

from app.analysis.indicators import IndicatorSet
from app.models import CandleSeries, MarketRegime, StructureBias, StructureState


def _slope(values: np.ndarray, window: int = 10) -> float:
    sample = values[-window:]
    if len(sample) < window or not np.all(np.isfinite(sample)) or abs(sample[0]) < 1e-12:
        return 0.0
    return float((sample[-1] - sample[0]) / abs(sample[0]))


def classify_regime(candles: CandleSeries, indicators: IndicatorSet, structure: StructureState) -> MarketRegime:
    close = candles.latest_close
    ema50, ema200 = float(indicators.ema50[-1]), float(indicators.ema200[-1])
    adx = float(indicators.adx[-1])
    natr = float(indicators.normalized_atr[-1])
    bb_width = indicators.bb_width
    finite_width = bb_width[np.isfinite(bb_width)]
    width = float(finite_width[-1]) if finite_width.size else np.nan
    width_p20 = float(np.percentile(finite_width[-100:], 20)) if finite_width.size >= 20 else np.nan
    natr_values = indicators.normalized_atr[np.isfinite(indicators.normalized_atr)]
    natr_p90 = float(np.percentile(natr_values[-100:], 90)) if natr_values.size >= 20 else np.inf
    if natr >= natr_p90 and natr > 0.025:
        return MarketRegime.HIGH_VOLATILITY
    if np.isfinite(width) and np.isfinite(width_p20) and width <= width_p20 and adx < 22:
        return MarketRegime.COMPRESSION
    bull = close > ema50 > ema200 and _slope(indicators.ema50) > 0 and _slope(indicators.ema200, 20) >= 0
    bear = close < ema50 < ema200 and _slope(indicators.ema50) < 0 and _slope(indicators.ema200, 20) <= 0
    if bull and adx >= 28 and structure.bias is StructureBias.BULLISH:
        return MarketRegime.STRONG_BULLISH_TREND
    if bear and adx >= 28 and structure.bias is StructureBias.BEARISH:
        return MarketRegime.STRONG_BEARISH_TREND
    if bull and adx >= 18:
        return MarketRegime.BULLISH_TREND
    if bear and adx >= 18:
        return MarketRegime.BEARISH_TREND
    if adx < 20 and structure.bias is StructureBias.RANGE:
        return MarketRegime.RANGE
    return MarketRegime.UNCLEAR
