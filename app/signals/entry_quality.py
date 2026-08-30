from __future__ import annotations

import math
from dataclasses import dataclass

from app.analysis.context import AnalysisContext, TimeframeAnalysis
from app.models import (
    Direction,
    EntryDecision,
    EntryQuality,
    SetupCandidate,
    SignalMode,
    StructureBias,
    TradePlan,
)
from app.signals.risk import room_to_target

ENTRY_CATEGORY_CAPS: dict[str, int] = {
    "location": 20,
    "retest": 20,
    "lower_timeframe_structure": 20,
    "stop_quality": 15,
    "room_to_target": 10,
    "momentum": 5,
    "volume": 5,
    "chase": 5,
}

RETEST_STRATEGY_TOKENS = (
    "BREAKOUT",
    "BREAKDOWN",
    "FLAG",
    "TRIANGLE",
    "WEDGE",
    "TRENDLINE",
    "BOS_CONTINUATION",
    "VWAP",
)


@dataclass(frozen=True, slots=True)
class EntryGateResult:
    actionable: bool
    wait: bool
    hard_reject: bool


def grade_entry_quality(total: int, hard_reasons: tuple[str, ...] = ()) -> EntryDecision:
    if hard_reasons or total < 65:
        return EntryDecision.REJECT
    if total < 75:
        return EntryDecision.WAIT
    if total < 85:
        return EntryDecision.VALID
    return EntryDecision.HIGH_QUALITY


def two_gate_result(setup_score: int, entry_quality: EntryQuality, minimum_setup: int = 80, minimum_entry: int = 75) -> EntryGateResult:
    hard_reject = bool(entry_quality.hard_reasons)
    actionable = not hard_reject and setup_score >= minimum_setup and entry_quality.total >= minimum_entry
    return EntryGateResult(actionable, not hard_reject and not actionable, hard_reject)


def _execution_timeframe(candidate: SetupCandidate) -> str:
    return "5m" if candidate.mode is SignalMode.SCALP else "15m"


def _distance_to_zone(price: float, low: float, high: float) -> float:
    if low <= price <= high:
        return 0.0
    return low - price if price < low else price - high


def _directional_reclaim(
    candidate: SetupCandidate,
    execution: TimeframeAnalysis,
    context: AnalysisContext,
) -> tuple[bool, bool, list[str]]:
    candles = context.snapshot.series[_execution_timeframe(candidate)]
    zone_low, zone_high = candidate.ideal_entry_low, candidate.ideal_entry_high
    lookback = min(4, len(candles))
    touched = bool(((candles.low[-lookback:] <= zone_high) & (candles.high[-lookback:] >= zone_low)).any())
    close = candles.latest_close
    bullish = candidate.direction is Direction.LONG
    held = close >= zone_low if bullish else close <= zone_high
    reclaimed = close >= (zone_low + zone_high) / 2 if bullish else close <= (zone_low + zone_high) / 2
    directional_candle = candles.close[-1] > candles.open[-1] if bullish else candles.close[-1] < candles.open[-1]
    sweep = any(event.direction is candidate.direction for event in execution.liquidity)
    evidence: list[str] = []
    if touched:
        evidence.append(f"{candles.timeframe.upper()} entry-zone retest occurred")
    if sweep and reclaimed:
        evidence.append("Liquidity sweep reclaimed the setup level")
    elif reclaimed and directional_candle:
        evidence.append("Retest held with a directional reclaim close")
    return touched, bool(held and (reclaimed or sweep) and directional_candle), evidence


def score_entry_quality(
    candidate: SetupCandidate,
    context: AnalysisContext,
    plan: TradePlan,
    *,
    current_price: float | None = None,
    max_chase_atr: float = 0.75,
) -> EntryQuality:
    execution_tf = _execution_timeframe(candidate)
    execution = context.timeframes[execution_tf]
    primary = context.timeframes[candidate.timeframe]
    price = context.snapshot.series[execution_tf].latest_close if current_price is None else current_price
    atr = float(execution.indicators.atr[-1])
    if not math.isfinite(atr) or atr <= 0:
        return EntryQuality(0, EntryDecision.REJECT, {}, (), ("Execution ATR is invalid",), ("INVALID_ATR",))

    categories: dict[str, int] = {}
    evidence: list[str] = []
    warnings: list[str] = []
    hard: list[str] = []
    distance_atr = _distance_to_zone(price, plan.entry_zone_low, plan.entry_zone_high) / atr
    inside = plan.entry_zone_low <= price <= plan.entry_zone_high
    if inside:
        categories["location"] = 20
        evidence.append("Price is inside the ideal entry zone")
    elif distance_atr <= 0.25:
        categories["location"] = 15
        warnings.append("Price is just outside the ideal entry zone")
    elif distance_atr <= 0.5:
        categories["location"] = 9
        warnings.append("Price has not returned fully to the ideal entry zone")
    elif distance_atr <= max_chase_atr:
        categories["location"] = 4
        warnings.append("Price is extended from the ideal entry zone")
    else:
        categories["location"] = 0

    adverse_extension = (
        candidate.direction is Direction.LONG and price > plan.entry_zone_high
    ) or (
        candidate.direction is Direction.SHORT and price < plan.entry_zone_low
    )
    if adverse_extension and distance_atr > max_chase_atr:
        warnings.append(f"Price is {distance_atr:.2f} ATR beyond the actionable entry zone")
        if distance_atr > max_chase_atr * 2.0:
            hard.append("ENTRY_TOO_LATE")

    touched, reclaim, retest_evidence = _directional_reclaim(candidate, execution, context)
    expects_retest = any(token in candidate.strategy for token in RETEST_STRATEGY_TOKENS)
    if touched and reclaim:
        categories["retest"] = 20
        evidence.extend(retest_evidence)
    elif touched:
        categories["retest"] = 11
        warnings.append("Entry zone was tested but has not produced a clean reclaim")
    elif expects_retest:
        categories["retest"] = 0
        warnings.append("The required breakout retest has not occurred")
    else:
        categories["retest"] = 7

    target_bias = StructureBias.BULLISH if candidate.direction is Direction.LONG else StructureBias.BEARISH
    matching_events = [event for event in execution.structure.events if event.direction is candidate.direction]
    matching_candle = any(
        item.direction is candidate.direction and item.index == len(context.snapshot.series[execution_tf]) - 1
        for item in execution.candlesticks
    )
    if matching_events and execution.structure.bias is target_bias:
        categories["lower_timeframe_structure"] = 20
        evidence.append(f"{execution_tf.upper()} {matching_events[-1].name} confirms directional structure")
    elif matching_events or (execution.structure.bias is target_bias and matching_candle):
        categories["lower_timeframe_structure"] = 15
        evidence.append(f"{execution_tf.upper()} structure responded in the trade direction")
    elif reclaim and (execution.momentum.direction is candidate.direction or matching_candle):
        categories["lower_timeframe_structure"] = 11
        evidence.append(f"{execution_tf.upper()} reclaim has supporting confirmation")
    elif execution.structure.bias is target_bias:
        categories["lower_timeframe_structure"] = 8
        warnings.append("Lower-timeframe bias aligns but no fresh BOS/CHoCH is confirmed")
    else:
        categories["lower_timeframe_structure"] = 0
        warnings.append("Lower-timeframe structure has not confirmed the entry")
    lower_confirmed = categories["lower_timeframe_structure"] >= 11

    primary_atr = float(primary.indicators.atr[-1])
    stop_atr = plan.risk_per_unit / primary_atr if primary_atr > 0 else math.inf
    beyond_invalidation = (
        candidate.direction is Direction.LONG and plan.stop_loss < candidate.invalidation_level
    ) or (
        candidate.direction is Direction.SHORT and plan.stop_loss > candidate.invalidation_level
    )
    if beyond_invalidation and 0.5 <= stop_atr <= 2.75:
        categories["stop_quality"] = 15
        evidence.append("Stop is beyond structural invalidation with an ATR buffer")
    elif beyond_invalidation and 0.3 <= stop_atr <= 3.5:
        categories["stop_quality"] = 10
        warnings.append("Stop geometry is acceptable but not ideal")
    else:
        categories["stop_quality"] = 0
        hard.append("NO_LOGICAL_STRUCTURAL_STOP")

    has_room = room_to_target(plan, primary.zones, candidate.direction, minimum_room_r=2.0)
    categories["room_to_target"] = 10 if has_room else 0
    if has_room:
        evidence.append("No major opposing structure blocks the planned 2R path")
    else:
        hard.append("INSUFFICIENT_ROOM_TO_TARGET")

    categories["momentum"] = 5 if execution.momentum.direction is candidate.direction else 2 if execution.momentum.direction is None else 0
    if categories["momentum"] == 5:
        evidence.append(f"{execution_tf.upper()} momentum confirms the entry")
    relative_volume = execution.volume.relative_volume
    volume_confirmed = execution.volume.direction is candidate.direction or relative_volume >= 1.2
    categories["volume"] = 5 if volume_confirmed else 2 if relative_volume >= 0.8 else 0
    if categories["volume"] == 5:
        evidence.append(f"{execution_tf.upper()} relative volume is {relative_volume:.2f}x")

    if distance_atr <= 0.15:
        categories["chase"] = 5
    elif distance_atr <= 0.35:
        categories["chase"] = 4
    elif distance_atr <= 0.5:
        categories["chase"] = 2
    elif distance_atr <= max_chase_atr:
        categories["chase"] = 1
    else:
        categories["chase"] = 0

    total = sum(min(ENTRY_CATEGORY_CAPS[name], max(0, categories.get(name, 0))) for name in ENTRY_CATEGORY_CAPS)
    hard_tuple = tuple(dict.fromkeys(hard))
    decision = grade_entry_quality(total, hard_tuple)
    return EntryQuality(
        total=total,
        decision=decision,
        categories=categories,
        evidence=tuple(dict.fromkeys(evidence)),
        warnings=tuple(dict.fromkeys(warnings)),
        hard_reasons=hard_tuple,
        retest_completed=touched and reclaim,
        lower_timeframe_confirmed=lower_confirmed,
        distance_from_entry_atr=distance_atr,
    )
