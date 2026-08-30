from __future__ import annotations

from app.analysis.context import AnalysisContext
from app.models import Direction, EntryPlan, SetupCandidate, ZoneKind


def calculate_entry_plan(candidate: SetupCandidate, context: AnalysisContext) -> EntryPlan:
    """Refine a detector's setup-specific zone without anchoring it to market price."""
    analysis = context.timeframes[candidate.timeframe]
    atr = float(analysis.indicators.atr[-1])
    low, high = candidate.ideal_entry_low, candidate.ideal_entry_high
    sources: list[str] = ["setup geometry"]

    retest_tokens = ("BREAKOUT", "BREAKDOWN", "FLAG", "TRIANGLE", "WEDGE", "TRENDLINE", "BOS_CONTINUATION")
    if any(token in candidate.strategy for token in retest_tokens):
        breakout_level = low if candidate.direction is Direction.LONG else high
        low = breakout_level - atr * 0.15
        high = breakout_level + atr * 0.15
        sources.append("broken structure retest level")

    relevant_kind = ZoneKind.SUPPORT if candidate.direction is Direction.LONG else ZoneKind.RESISTANCE
    overlaps = [
        zone
        for zone in analysis.zones
        if zone.kind in {relevant_kind, ZoneKind.MIXED}
        and zone.low <= high + atr * 0.25
        and zone.high >= low - atr * 0.25
    ]
    if overlaps:
        best = max(overlaps, key=lambda zone: (zone.score, zone.reactions, zone.last_index))
        overlap_low = max(low, best.low)
        overlap_high = min(high, best.high)
        if overlap_low <= overlap_high:
            low, high = overlap_low, overlap_high
        else:
            level = best.midpoint
            low = max(low, level - atr * 0.12)
            high = min(high, level + atr * 0.12)
        sources.extend(best.sources[:2])

    # EMA pullbacks use the actual EMA value as a location input, never as an
    # independent confirmation count.
    if "EMA_PULLBACK" in candidate.strategy:
        ema = float(analysis.indicators.ema20[-1])
        low = max(low, ema - atr * 0.18)
        high = min(high, ema + atr * 0.18)
        sources.append("EMA20 pullback area")

    if low > high:
        low, high = candidate.ideal_entry_low, candidate.ideal_entry_high
    preferred = (low + high) / 2.0
    entry_type = candidate.strategy.lower().replace("_", " ")
    return EntryPlan(low, high, preferred, entry_type, candidate.trigger, tuple(dict.fromkeys(sources)))
