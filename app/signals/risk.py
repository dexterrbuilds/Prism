from __future__ import annotations

from app.models import Direction, SetupCandidate, SupportResistanceZone, TradePlan


class RiskPlanningError(ValueError):
    pass


def estimate_hold_time(strategy: str, target_distance_atr: float) -> tuple[float, float]:
    """Technical horizon estimate, not an empirical performance statistic."""
    if target_distance_atr <= 0:
        raise RiskPlanningError("target ATR distance must be positive")
    if any(token in strategy for token in ("REVERSAL", "HEAD_AND_SHOULDERS", "DOUBLE_")):
        hours_per_atr = 4.5
    elif any(token in strategy for token in ("BREAKOUT", "BREAKDOWN", "MOMENTUM", "BOS_")):
        hours_per_atr = 2.75
    elif "PULLBACK" in strategy or "RETEST" in strategy:
        hours_per_atr = 3.5
    else:
        hours_per_atr = 3.25
    center = target_distance_atr * hours_per_atr
    low = max(4.0, min(72.0, round(center * 0.65)))
    high = max(low + 2.0, min(120.0, round(center * 1.6)))
    return low, high


def two_r_target(entry: float, stop: float, direction: Direction) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        raise RiskPlanningError("stop must differ from entry")
    return entry + 2 * risk if direction is Direction.LONG else entry - 2 * risk


def is_entry_too_late(current_price: float, candidate: SetupCandidate, atr: float, max_chase_atr: float = 0.75) -> bool:
    if candidate.ideal_entry_low <= current_price <= candidate.ideal_entry_high:
        return False
    distance = candidate.ideal_entry_low - current_price if current_price < candidate.ideal_entry_low else current_price - candidate.ideal_entry_high
    adverse_chase = (candidate.direction is Direction.LONG and current_price > candidate.ideal_entry_high) or (
        candidate.direction is Direction.SHORT and current_price < candidate.ideal_entry_low
    )
    return adverse_chase and distance > atr * max_chase_atr


def _opposing_levels(zones: tuple[SupportResistanceZone, ...], entry: float, direction: Direction) -> list[SupportResistanceZone]:
    if direction is Direction.LONG:
        return sorted((zone for zone in zones if zone.midpoint > entry), key=lambda zone: zone.midpoint)
    return sorted((zone for zone in zones if zone.midpoint < entry), key=lambda zone: zone.midpoint, reverse=True)


def build_trade_plan(
    candidate: SetupCandidate,
    current_price: float,
    atr: float,
    zones: tuple[SupportResistanceZone, ...],
) -> TradePlan:
    if atr <= 0:
        raise RiskPlanningError("ATR must be positive")
    midpoint = (candidate.ideal_entry_low + candidate.ideal_entry_high) / 2
    preferred = current_price if candidate.ideal_entry_low <= current_price <= candidate.ideal_entry_high else midpoint
    if candidate.direction is Direction.LONG:
        stop = min(candidate.invalidation_level, candidate.ideal_entry_low) - atr * 0.15
        if stop >= preferred:
            raise RiskPlanningError("long stop is not below entry")
    else:
        stop = max(candidate.invalidation_level, candidate.ideal_entry_high) + atr * 0.15
        if stop <= preferred:
            raise RiskPlanningError("short stop is not above entry")
    risk = abs(preferred - stop)
    tp2 = two_r_target(preferred, stop, candidate.direction)
    opposing = _opposing_levels(zones, preferred, candidate.direction)
    meaningful = [zone for zone in opposing if zone.score >= 3.0]
    tp1 = meaningful[0].midpoint if meaningful else (preferred + risk if candidate.direction is Direction.LONG else preferred - risk)
    tp3 = meaningful[1].midpoint if len(meaningful) > 1 else None
    hold_low, hold_high = estimate_hold_time(candidate.strategy, abs(tp2 - preferred) / atr)
    return TradePlan(
        candidate.ideal_entry_low,
        candidate.ideal_entry_high,
        preferred,
        candidate.strategy.lower().replace("_", " "),
        candidate.trigger,
        stop,
        risk,
        risk / atr,
        f"{candidate.timeframe} structure invalidated beyond {candidate.invalidation_level:.8g}",
        tp1,
        tp2,
        tp3,
        2.0,
        hold_low,
        hold_high,
    )


def room_to_target(plan: TradePlan, zones: tuple[SupportResistanceZone, ...], direction: Direction, minimum_room_r: float = 1.5) -> bool:
    for zone in _opposing_levels(zones, plan.preferred_entry, direction):
        if zone.score < 5.0:
            continue
        room = abs(zone.midpoint - plan.preferred_entry) / plan.risk_per_unit
        return room >= minimum_room_r
    return True
