from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.analysis.indicators import IndicatorSet
from app.models import StructureState, SupportResistanceZone, SwingKind, ZoneKind


@dataclass(frozen=True, slots=True)
class _Level:
    price: float
    source: str
    index: int
    kind: ZoneKind
    weight: float


def cluster_levels(
    structure: StructureState,
    indicators: IndicatorSet,
    current_price: float,
    atr: float,
    tolerance_atr: float = 0.5,
) -> tuple[SupportResistanceZone, ...]:
    levels: list[_Level] = []
    for swing in structure.swings[-30:]:
        kind = ZoneKind.RESISTANCE if swing.kind is SwingKind.HIGH else ZoneKind.SUPPORT
        recency = 1.0 + 1.5 * max(0.0, swing.index / max(1, structure.swings[-1].index))
        levels.append(_Level(swing.price, f"swing_{swing.label.value}", swing.index, kind, recency))
    calendar = (
        (structure.previous_day_high, "previous_day_high", ZoneKind.RESISTANCE, 2.5),
        (structure.previous_day_low, "previous_day_low", ZoneKind.SUPPORT, 2.5),
        (structure.previous_week_high, "previous_week_high", ZoneKind.RESISTANCE, 3.0),
        (structure.previous_week_low, "previous_week_low", ZoneKind.SUPPORT, 3.0),
        (structure.range_high, "range_high", ZoneKind.RESISTANCE, 2.0),
        (structure.range_low, "range_low", ZoneKind.SUPPORT, 2.0),
    )
    last_index = structure.swings[-1].index if structure.swings else 0
    for price, source, kind, weight in calendar:
        if price is not None:
            levels.append(_Level(price, source, last_index, kind, weight))
    for name in ("ema20", "ema50", "ema100", "ema200"):
        price = float(getattr(indicators, name)[-1])
        if np.isfinite(price):
            kind = ZoneKind.SUPPORT if price <= current_price else ZoneKind.RESISTANCE
            levels.append(_Level(price, name, last_index, kind, 1.0 if name in {"ema20", "ema100"} else 1.5))
    levels.sort(key=lambda level: level.price)
    tolerance = max(atr * tolerance_atr, current_price * 0.0005)
    clusters: list[list[_Level]] = []
    for level in levels:
        if clusters and abs(level.price - np.average([x.price for x in clusters[-1]], weights=[x.weight for x in clusters[-1]])) <= tolerance:
            clusters[-1].append(level)
        else:
            clusters.append([level])
    zones: list[SupportResistanceZone] = []
    for cluster in clusters:
        kinds = {level.kind for level in cluster}
        kind = next(iter(kinds)) if len(kinds) == 1 else ZoneKind.MIXED
        score = min(10.0, sum(level.weight for level in cluster) + max(0, len(cluster) - 1))
        zones.append(
            SupportResistanceZone(
                low=min(level.price for level in cluster) - tolerance * 0.15,
                high=max(level.price for level in cluster) + tolerance * 0.15,
                kind=kind,
                score=score,
                reactions=len(cluster),
                sources=tuple(dict.fromkeys(level.source for level in cluster)),
                last_index=max(level.index for level in cluster),
            )
        )
    return tuple(sorted(zones, key=lambda zone: zone.score, reverse=True)[:16])


def nearest_zone(zones: tuple[SupportResistanceZone, ...], price: float, below: bool) -> SupportResistanceZone | None:
    candidates = [zone for zone in zones if (zone.midpoint < price if below else zone.midpoint > price)]
    return min(candidates, key=lambda zone: abs(zone.midpoint - price), default=None)
