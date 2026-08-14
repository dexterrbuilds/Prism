from __future__ import annotations

import numpy as np

from app.analysis.context import AnalysisContext, TimeframeAnalysis
from app.analysis.indicators import IndicatorSet
from app.analysis.momentum import MomentumState
from app.analysis.volume import VolumeState
from app.models import (
    CandleSeries,
    MarketRegime,
    MarketSnapshot,
    StructureBias,
    StructureState,
    VolatilityClass,
)


def candles(
    close: np.ndarray | list[float],
    timeframe: str = "1h",
    *,
    high: np.ndarray | None = None,
    low: np.ndarray | None = None,
    open_: np.ndarray | None = None,
    volume: np.ndarray | None = None,
) -> CandleSeries:
    c = np.asarray(close, dtype=np.float64)
    interval = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[timeframe]
    timestamp = np.arange(len(c), dtype=np.int64) * interval
    o = np.asarray(open_, dtype=np.float64) if open_ is not None else c.copy()
    h = np.asarray(high, dtype=np.float64) if high is not None else np.maximum(o, c) + 1.0
    low_values = np.asarray(low, dtype=np.float64) if low is not None else np.minimum(o, c) - 1.0
    v = np.asarray(volume, dtype=np.float64) if volume is not None else np.full(len(c), 100.0)
    return CandleSeries("BTC/USDT", timeframe, timestamp, o, h, low_values, c, v, int(timestamp[-1] + interval))


def indicators(size: int = 250, *, price: float = 100.0, atr: float = 2.0, rsi: np.ndarray | None = None) -> IndicatorSet:
    def const(value: float) -> np.ndarray:
        return np.full(size, value, dtype=np.float64)
    return IndicatorSet(
        const(price - 1), const(price - 2), const(price - 3), const(price - 4), const(price - 2), const(price - 4),
        const(25), const(30), const(15), const(atr), const(atr / price), rsi if rsi is not None else const(55),
        const(1), const(0.5), const(0.5), const(60), const(55), const(1), const(50),
        const(price + 4), const(price), const(price - 4), const(0.08), const(100), const(1.5),
        np.linspace(1, 2, size), const(55), np.linspace(1, 2, size),
    )


def analysis_context(
    one_hour: CandleSeries,
    structure: StructureState,
    *,
    regime: MarketRegime = MarketRegime.BULLISH_TREND,
    zones=(),
    momentum_direction=None,
    volume_direction=None,
    relative_volume: float = 1.5,
) -> AnalysisContext:
    size = len(one_hour)
    ind = indicators(size, price=one_hour.latest_close)
    timeframe_analysis = TimeframeAnalysis(
        indicators=ind,
        structure=structure,
        zones=zones,
        momentum=MomentumState(momentum_direction, 0.8, ("momentum confirmation",) if momentum_direction else (), False),
        volume=VolumeState(volume_direction, relative_volume, relative_volume >= 1.2, False, ("Relative volume expansion",)),
        volatility=VolatilityClass.NORMAL,
        candlesticks=(), patterns=(), divergences=(), liquidity=(),
    )
    lower_structure = StructureState(StructureBias.BULLISH, (), (), None, None, None, None)
    lower = TimeframeAnalysis(
        indicators=indicators(size, price=one_hour.latest_close), structure=lower_structure, zones=(),
        momentum=MomentumState(momentum_direction, 0.8, ("15M momentum confirmation",) if momentum_direction else (), False),
        volume=VolumeState(volume_direction, relative_volume, True, False, ()), volatility=VolatilityClass.NORMAL,
        candlesticks=(), patterns=(), divergences=(), liquidity=(),
    )
    four = TimeframeAnalysis(
        indicators=indicators(size, price=one_hour.latest_close), structure=structure, zones=zones,
        momentum=timeframe_analysis.momentum, volume=timeframe_analysis.volume, volatility=VolatilityClass.NORMAL,
        candlesticks=(), patterns=(), divergences=(), liquidity=(),
    )
    series = {
        "1h": one_hour,
        "15m": candles(one_hour.close, "15m"),
        "4h": candles(one_hour.close, "4h"),
    }
    return AnalysisContext(MarketSnapshot("BTC/USDT", series, one_hour.as_of_ms), regime, {"1h": timeframe_analysis, "15m": lower, "4h": four})
