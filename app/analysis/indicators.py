from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import talib
from talib._ta_lib import MA_Type

from app.models import CandleSeries, FloatArray


@dataclass(frozen=True, slots=True)
class IndicatorSet:
    ema20: FloatArray
    ema50: FloatArray
    ema100: FloatArray
    ema200: FloatArray
    sma50: FloatArray
    sma200: FloatArray
    adx: FloatArray
    plus_di: FloatArray
    minus_di: FloatArray
    atr: FloatArray
    normalized_atr: FloatArray
    rsi: FloatArray
    macd: FloatArray
    macd_signal: FloatArray
    macd_hist: FloatArray
    stoch_rsi_k: FloatArray
    stoch_rsi_d: FloatArray
    roc: FloatArray
    cci: FloatArray
    bb_upper: FloatArray
    bb_middle: FloatArray
    bb_lower: FloatArray
    bb_width: FloatArray
    volume_sma20: FloatArray
    relative_volume: FloatArray
    obv: FloatArray
    mfi: FloatArray
    ad: FloatArray


def _safe_divide(numerator: FloatArray, denominator: FloatArray) -> FloatArray:
    result = np.full_like(numerator, np.nan, dtype=np.float64)
    np.divide(numerator, denominator, out=result, where=np.abs(denominator) > 1e-12)
    return result


def calculate_indicators(candles: CandleSeries) -> IndicatorSet:
    h, low, c, v = candles.high, candles.low, candles.close, candles.volume
    ema20 = talib.EMA(c, 20)
    ema50 = talib.EMA(c, 50)
    ema100 = talib.EMA(c, 100)
    ema200 = talib.EMA(c, 200)
    sma50 = talib.SMA(c, 50)
    sma200 = talib.SMA(c, 200)
    adx = talib.ADX(h, low, c, 14)
    plus_di = talib.PLUS_DI(h, low, c, 14)
    minus_di = talib.MINUS_DI(h, low, c, 14)
    atr = talib.ATR(h, low, c, 14)
    normalized_atr = _safe_divide(atr, c)
    rsi = talib.RSI(c, 14)
    macd, macd_signal, macd_hist = talib.MACD(c, 12, 26, 9)
    stoch_k, stoch_d = talib.STOCHRSI(c, 14, 5, 3, MA_Type.SMA)
    roc = talib.ROC(c, 10)
    cci = talib.CCI(h, low, c, 14)
    upper, middle, lower = talib.BBANDS(c, 20, 2.0, 2.0, MA_Type.SMA)
    bb_width = _safe_divide(upper - lower, middle)
    volume_sma20 = talib.SMA(v, 20)
    relative_volume = _safe_divide(v, volume_sma20)
    return IndicatorSet(
        ema20, ema50, ema100, ema200, sma50, sma200, adx, plus_di, minus_di,
        atr, normalized_atr, rsi, macd, macd_signal, macd_hist, stoch_k, stoch_d,
        roc, cci, upper, middle, lower, bb_width, volume_sma20, relative_volume,
        talib.OBV(c, v), talib.MFI(h, low, c, v, 14), talib.AD(h, low, c, v),
    )


def indicators_valid(indicators: IndicatorSet, minimum_finite: int = 20) -> bool:
    required = (
        indicators.ema200,
        indicators.adx,
        indicators.atr,
        indicators.rsi,
        indicators.macd_hist,
        indicators.bb_width,
    )
    return all(np.count_nonzero(np.isfinite(values)) >= minimum_finite for values in required) and bool(
        np.isfinite(indicators.atr[-1]) and indicators.atr[-1] > 0
    )
