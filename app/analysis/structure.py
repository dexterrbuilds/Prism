from __future__ import annotations

import numpy as np

from app.models import (
    CandleSeries,
    Direction,
    StructureBias,
    StructureEvent,
    StructureState,
    SwingKind,
    SwingLabel,
    SwingPoint,
)


def detect_swings(candles: CandleSeries, left: int = 3, right: int = 3) -> tuple[SwingPoint, ...]:
    """Confirmed pivots only; pivot i becomes usable at i + right."""
    raw: list[tuple[int, SwingKind, float]] = []
    for index in range(left, len(candles) - right):
        high_window = candles.high[index - left : index + right + 1]
        low_window = candles.low[index - left : index + right + 1]
        if candles.high[index] == np.max(high_window) and np.count_nonzero(high_window == candles.high[index]) == 1:
            raw.append((index, SwingKind.HIGH, float(candles.high[index])))
        if candles.low[index] == np.min(low_window) and np.count_nonzero(low_window == candles.low[index]) == 1:
            raw.append((index, SwingKind.LOW, float(candles.low[index])))
    raw.sort(key=lambda value: (value[0], value[1].value))
    points: list[SwingPoint] = []
    previous: dict[SwingKind, float] = {}
    for index, kind, price in raw:
        prior = previous.get(kind)
        if prior is None:
            label = SwingLabel.HIGH if kind is SwingKind.HIGH else SwingLabel.LOW
        elif kind is SwingKind.HIGH:
            label = SwingLabel.HH if price > prior else SwingLabel.LH
        else:
            label = SwingLabel.HL if price > prior else SwingLabel.LL
        points.append(SwingPoint(index, int(candles.timestamp[index]), price, kind, label, index + right))
        previous[kind] = price
    return tuple(points)


def _bias(swings: tuple[SwingPoint, ...]) -> StructureBias:
    highs = [s for s in swings if s.kind is SwingKind.HIGH][-2:]
    lows = [s for s in swings if s.kind is SwingKind.LOW][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return StructureBias.UNCLEAR
    if highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price:
        return StructureBias.BULLISH
    if highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price:
        return StructureBias.BEARISH
    return StructureBias.RANGE


def detect_structure(candles: CandleSeries, left: int = 3, right: int = 3) -> StructureState:
    swings = detect_swings(candles, left, right)
    events: list[StructureEvent] = []
    prior_bias = _bias(swings[:-2])
    highs = [s for s in swings if s.kind is SwingKind.HIGH]
    lows = [s for s in swings if s.kind is SwingKind.LOW]
    if len(candles) >= 2:
        for swing, direction in ((highs[-1] if highs else None, Direction.LONG), (lows[-1] if lows else None, Direction.SHORT)):
            if swing is None or swing.confirmed_at_index >= len(candles) - 1:
                continue
            previous_close, close = float(candles.close[-2]), float(candles.close[-1])
            crossed = previous_close <= swing.price < close if direction is Direction.LONG else previous_close >= swing.price > close
            if crossed:
                against = (direction is Direction.LONG and prior_bias is StructureBias.BEARISH) or (
                    direction is Direction.SHORT and prior_bias is StructureBias.BULLISH
                )
                events.append(StructureEvent("CHOCH" if against else "BOS", direction, len(candles) - 1, swing.price))
        # A wick through a confirmed pivot with a close back inside is a failed break/reclaim,
        # not a BOS. It is recorded separately so strategies cannot confuse the two.
        if highs:
            level = highs[-1].price
            if candles.high[-1] > level and candles.close[-1] < level:
                events.append(StructureEvent("FAILED_BREAK_RECLAIM", Direction.SHORT, len(candles) - 1, level))
        if lows:
            level = lows[-1].price
            if candles.low[-1] < level and candles.close[-1] > level:
                events.append(StructureEvent("FAILED_BREAK_RECLAIM", Direction.LONG, len(candles) - 1, level))
    recent = swings[-12:]
    recent_highs = [s.price for s in recent if s.kind is SwingKind.HIGH]
    recent_lows = [s.price for s in recent if s.kind is SwingKind.LOW]
    return StructureState(
        bias=_bias(swings),
        swings=swings,
        events=tuple(events),
        significant_high=highs[-1].price if highs else None,
        significant_low=lows[-1].price if lows else None,
        range_high=max(recent_highs) if recent_highs else None,
        range_low=min(recent_lows) if recent_lows else None,
    )


def add_calendar_levels(structure: StructureState, candles_4h: CandleSeries) -> StructureState:
    """Derive previous completed UTC day/week from bounded 4H candles."""
    if len(candles_4h) == 0:
        return structure
    day = 86_400_000
    week = 7 * day
    last_open = int(candles_4h.timestamp[-1])
    current_day = last_open // day
    # Unix epoch began on Thursday; shifting three days produces Monday UTC buckets.
    current_week = (last_open + 3 * day) // week
    day_mask = candles_4h.timestamp // day == current_day - 1
    week_mask = (candles_4h.timestamp + 3 * day) // week == current_week - 1

    def extrema(mask: np.ndarray) -> tuple[float | None, float | None]:
        if not np.any(mask):
            return None, None
        return float(np.max(candles_4h.high[mask])), float(np.min(candles_4h.low[mask]))

    pdh, pdl = extrema(day_mask)
    pwh, pwl = extrema(week_mask)
    return StructureState(
        structure.bias, structure.swings, structure.events, structure.significant_high,
        structure.significant_low, structure.range_high, structure.range_low, pdh, pdl, pwh, pwl,
    )
