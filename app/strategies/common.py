from __future__ import annotations

from app.analysis.context import AnalysisContext
from app.models import ConfluenceEvidence, Direction, SetupCandidate, SupportResistanceZone


def confirmation(context: AnalysisContext, direction: Direction) -> tuple[bool, list[str]]:
    lower = context.timeframes["15m"]
    evidence: list[str] = []
    if lower.momentum.direction is direction:
        evidence.extend(lower.momentum.evidence[:1])
    matching = [item for item in lower.candlesticks if item.direction is direction and item.index == len(context.snapshot.series["15m"]) - 1]
    if matching:
        evidence.append(f"15M {matching[-1].name.lower()} confirmation")
    event = next((event for event in lower.structure.events if event.direction is direction), None)
    if event:
        evidence.append(f"15M {event.name} confirmation")
    return bool(evidence), evidence


def candidate(
    context: AnalysisContext,
    strategy: str,
    direction: Direction,
    zone_low: float,
    zone_high: float,
    invalidation: float,
    trigger: str,
    quality: float,
    evidence: ConfluenceEvidence,
    confirmed: bool,
    **metadata: float | str | bool,
) -> SetupCandidate:
    return SetupCandidate(
        symbol=context.snapshot.symbol,
        strategy=strategy,
        direction=direction,
        timeframe="1h",
        detected_at_ms=context.snapshot.as_of_ms,
        ideal_entry_low=min(zone_low, zone_high),
        ideal_entry_high=max(zone_low, zone_high),
        trigger=trigger,
        invalidation_level=invalidation,
        quality=max(0.0, min(1.0, quality)),
        evidence=evidence,
        confirmed=confirmed,
        metadata=metadata,
    )


def zone_evidence(zone: SupportResistanceZone) -> list[str]:
    label = ", ".join(zone.sources[:2]).replace("_", " ")
    return [f"Price reacting at {label} zone"]
