from __future__ import annotations

from dataclasses import dataclass

from app.models import CandleSeries, Direction, StructureState, SwingKind


@dataclass(frozen=True, slots=True)
class LiquidityEvent:
    name: str
    direction: Direction
    level: float
    quality: float
    evidence: tuple[str, ...]


def detect_liquidity_events(candles: CandleSeries, structure: StructureState, atr: float) -> tuple[LiquidityEvent, ...]:
    if len(candles) < 3 or atr <= 0:
        return ()
    events: list[LiquidityEvent] = []
    o, h, low, close = map(float, (candles.open[-1], candles.high[-1], candles.low[-1], candles.close[-1]))
    highs = [s for s in structure.swings if s.kind is SwingKind.HIGH and s.confirmed_at_index < len(candles) - 1]
    lows = [s for s in structure.swings if s.kind is SwingKind.LOW and s.confirmed_at_index < len(candles) - 1]
    reference_highs = [(s.price, "swing high") for s in highs[-4:]]
    reference_lows = [(s.price, "swing low") for s in lows[-4:]]
    if len(highs) >= 2 and abs(highs[-1].price - highs[-2].price) <= atr * 0.25:
        reference_highs.append(((highs[-1].price + highs[-2].price) / 2, "equal highs"))
    if len(lows) >= 2 and abs(lows[-1].price - lows[-2].price) <= atr * 0.25:
        reference_lows.append(((lows[-1].price + lows[-2].price) / 2, "equal lows"))
    for value, name in (
        (structure.previous_day_high, "previous high"),
        (structure.range_high, "range high"),
    ):
        if value is not None:
            reference_highs.append((value, name))
    for value, name in (
        (structure.previous_day_low, "previous low"),
        (structure.range_low, "range low"),
    ):
        if value is not None:
            reference_lows.append((value, name))
    tolerance = max(atr * 0.1, close * 0.0003)
    for level, source in reference_lows:
        if low < level - tolerance and close > level and close > o:
            penetration = min(1.0, (level - low) / atr)
            events.append(LiquidityEvent("previous-low sweep / support reclaim", Direction.LONG, level, 0.65 + 0.25 * penetration, (f"{source} swept and reclaimed",)))
    for level, source in reference_highs:
        if h > level + tolerance and close < level and close < o:
            penetration = min(1.0, (h - level) / atr)
            events.append(LiquidityEvent("previous-high sweep / resistance reclaim", Direction.SHORT, level, 0.65 + 0.25 * penetration, (f"{source} swept and rejected",)))
    # A close beyond a level followed immediately by a close back inside is a failed break/trap.
    previous_close, prior_close = float(candles.close[-2]), float(candles.close[-3])
    for level, source in reference_highs:
        if prior_close <= level < previous_close and close < level:
            events.append(LiquidityEvent("failed breakout / bull trap", Direction.SHORT, level, 0.85, (f"failed break above {source}",)))
    for level, source in reference_lows:
        if prior_close >= level > previous_close and close > level:
            events.append(LiquidityEvent("failed breakdown / bear trap", Direction.LONG, level, 0.85, (f"failed break below {source}",)))
    return tuple(events)
