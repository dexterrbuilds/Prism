from __future__ import annotations

from dataclasses import dataclass

from app.analysis.context import AnalysisContext
from app.models import Direction, MarketRegime, SetupCandidate, SignalGrade, SignalMode, StructureBias

CATEGORY_CAPS: dict[str, int] = {
    "trend": 20,
    "structure": 20,
    "location": 15,
    "momentum": 10,
    "volume": 10,
    "pattern": 10,
    "candlestick": 5,
    "volatility": 5,
    "higher_timeframe": 5,
}


@dataclass(frozen=True, slots=True)
class ScoreResult:
    total: int
    grade: SignalGrade
    categories: dict[str, int]
    evidence: tuple[str, ...]


def grade_score(score: int) -> SignalGrade:
    if score >= 90:
        return SignalGrade.EXCEPTIONAL
    if score >= 80:
        return SignalGrade.VALID
    if score >= 70:
        return SignalGrade.WATCH
    return SignalGrade.IGNORE


def _aligned_regime(regime: MarketRegime, direction: Direction) -> tuple[float, str]:
    strong = MarketRegime.STRONG_BULLISH_TREND if direction is Direction.LONG else MarketRegime.STRONG_BEARISH_TREND
    moderate = MarketRegime.BULLISH_TREND if direction is Direction.LONG else MarketRegime.BEARISH_TREND
    opposing = {MarketRegime.STRONG_BEARISH_TREND, MarketRegime.BEARISH_TREND} if direction is Direction.LONG else {MarketRegime.STRONG_BULLISH_TREND, MarketRegime.BULLISH_TREND}
    if regime is strong:
        return 1.0, f"4H {regime.value.lower().replace('_', ' ')}"
    if regime is moderate:
        return 0.85, f"4H {regime.value.lower().replace('_', ' ')}"
    if regime in opposing:
        return 0.1, "Setup is counter to 4H trend"
    if regime in {MarketRegime.RANGE, MarketRegime.COMPRESSION}:
        return 0.6, f"4H {regime.value.lower()} context"
    return 0.35, "4H regime is not directional"


def score_candidate(candidate: SetupCandidate, context: AnalysisContext) -> ScoreResult:
    primary_tf = "15m" if candidate.mode is SignalMode.SCALP else "1h"
    lower_tf = "5m" if candidate.mode is SignalMode.SCALP else "15m"
    primary = context.timeframes[primary_tf]
    lower = context.timeframes[lower_tf]
    evidence_map = candidate.evidence.as_mapping()
    categories: dict[str, int] = {}
    actual: list[str] = []
    regime_strength, regime_text = _aligned_regime(context.regime, candidate.direction)
    categories["trend"] = round(CATEGORY_CAPS["trend"] * regime_strength)
    actual.append(regime_text)

    target_bias = StructureBias.BULLISH if candidate.direction is Direction.LONG else StructureBias.BEARISH
    structure_strength = 0.65 if primary.structure.bias is target_bias else 0.3 if primary.structure.bias is StructureBias.RANGE else 0.1
    if any(event.direction is candidate.direction for event in primary.structure.events):
        structure_strength = min(1.0, structure_strength + 0.3)
    if evidence_map["structure"]:
        structure_strength = max(structure_strength, 0.65)
    categories["structure"] = round(CATEGORY_CAPS["structure"] * structure_strength)
    if primary.structure.bias is target_bias:
        actual.append(f"{primary_tf.upper()} {target_bias.value.lower()} swing structure")

    location_strength = 0.8 if evidence_map["location"] else 0.25
    near_quality_zone = any(zone.low - float(primary.indicators.atr[-1]) * 0.2 <= candidate.ideal_entry_high and zone.high + float(primary.indicators.atr[-1]) * 0.2 >= candidate.ideal_entry_low and zone.score >= 4 for zone in primary.zones)
    if near_quality_zone:
        location_strength = 1.0
        actual.append("Entry overlaps a scored support/resistance zone")
    categories["location"] = round(CATEGORY_CAPS["location"] * location_strength)

    if primary.momentum.direction is candidate.direction:
        momentum_strength = 0.9
    elif primary.momentum.direction is None:
        momentum_strength = 0.45
    else:
        momentum_strength = 0.1
    categories["momentum"] = round(CATEGORY_CAPS["momentum"] * momentum_strength)
    if primary.momentum.direction is candidate.direction:
        actual.extend(primary.momentum.evidence[:1])

    rv = primary.volume.relative_volume
    volume_strength = 0.35
    if primary.volume.direction is candidate.direction:
        volume_strength += 0.25
    if rv >= 1.2:
        volume_strength += 0.3
    categories["volume"] = round(CATEGORY_CAPS["volume"] * min(1.0, volume_strength))
    if rv >= 1.2:
        actual.append(f"{primary_tf.upper()} relative volume {rv:.2f}x")

    categories["pattern"] = round(CATEGORY_CAPS["pattern"] * candidate.quality)
    lower_price = context.snapshot.series[lower_tf].latest_close
    lower_atr = float(lower.indicators.atr[-1])
    candle_at_location = candidate.ideal_entry_low - lower_atr * 0.35 <= lower_price <= candidate.ideal_entry_high + lower_atr * 0.35
    matching_candle = candle_at_location and any(item.direction is candidate.direction and item.index == len(context.snapshot.series[lower_tf]) - 1 for item in lower.candlesticks)
    categories["candlestick"] = CATEGORY_CAPS["candlestick"] if matching_candle else (2 if candidate.confirmed else 0)
    volatility_strength = 0.8 if primary.volatility.value in {"NORMAL", "EXPANDING"} else 0.45
    categories["volatility"] = round(CATEGORY_CAPS["volatility"] * volatility_strength)
    actual.append(f"{primary_tf.upper()} volatility is {primary.volatility.value.lower()}")
    categories["higher_timeframe"] = round(CATEGORY_CAPS["higher_timeframe"] * regime_strength)

    for name in CATEGORY_CAPS:
        actual.extend(evidence_map[name])
    total = min(100, sum(min(CATEGORY_CAPS[name], max(0, value)) for name, value in categories.items()))
    # Unclear context requires stronger proof; it cannot be exceptional in V1.
    if context.regime is MarketRegime.UNCLEAR:
        total = min(total, 84)
    return ScoreResult(total, grade_score(total), categories, tuple(dict.fromkeys(actual)))
