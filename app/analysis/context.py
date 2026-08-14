from __future__ import annotations

from dataclasses import dataclass

from app.analysis.candlesticks import CandlestickDetection, detect_candlesticks
from app.analysis.divergence import DivergenceDetection, detect_divergences
from app.analysis.indicators import IndicatorSet, calculate_indicators, indicators_valid
from app.analysis.liquidity import LiquidityEvent, detect_liquidity_events
from app.analysis.momentum import MomentumState, evaluate_momentum
from app.analysis.patterns import detect_chart_patterns
from app.analysis.regime import classify_regime
from app.analysis.structure import add_calendar_levels, detect_structure
from app.analysis.support_resistance import cluster_levels
from app.analysis.volatility import classify_volatility
from app.analysis.volume import VolumeState, evaluate_volume
from app.models import MarketRegime, MarketSnapshot, PatternDetection, StructureState, SupportResistanceZone, VolatilityClass


@dataclass(frozen=True, slots=True)
class TimeframeAnalysis:
    indicators: IndicatorSet
    structure: StructureState
    zones: tuple[SupportResistanceZone, ...]
    momentum: MomentumState
    volume: VolumeState
    volatility: VolatilityClass
    candlesticks: tuple[CandlestickDetection, ...]
    patterns: tuple[PatternDetection, ...]
    divergences: tuple[DivergenceDetection, ...]
    liquidity: tuple[LiquidityEvent, ...]


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    snapshot: MarketSnapshot
    regime: MarketRegime
    timeframes: dict[str, TimeframeAnalysis]


def analyze_snapshot(snapshot: MarketSnapshot, pivot_left: int = 3, pivot_right: int = 3, zone_tolerance: float = 0.5) -> AnalysisContext:
    analyses: dict[str, TimeframeAnalysis] = {}
    candles_4h = snapshot.series["4h"]
    for timeframe, candles in snapshot.series.items():
        indicators = calculate_indicators(candles)
        if not indicators_valid(indicators):
            raise ValueError(f"invalid indicators for {timeframe}")
        structure = detect_structure(candles, pivot_left, pivot_right)
        structure = add_calendar_levels(structure, candles_4h)
        atr = float(indicators.atr[-1])
        zones = cluster_levels(structure, indicators, candles.latest_close, atr, zone_tolerance)
        analyses[timeframe] = TimeframeAnalysis(
            indicators=indicators,
            structure=structure,
            zones=zones,
            momentum=evaluate_momentum(indicators),
            volume=evaluate_volume(indicators),
            volatility=classify_volatility(indicators),
            candlesticks=detect_candlesticks(candles),
            patterns=detect_chart_patterns(candles, structure, atr),
            divergences=detect_divergences(structure, indicators),
            liquidity=detect_liquidity_events(candles, structure, atr),
        )
    macro = analyses["4h"]
    regime = classify_regime(candles_4h, macro.indicators, macro.structure)
    return AnalysisContext(snapshot, regime, analyses)
