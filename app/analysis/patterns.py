from __future__ import annotations

import numpy as np

from app.models import CandleSeries, Direction, PatternDetection, StructureState, SwingKind


def _relative_spread(values: list[float]) -> float:
    return (max(values) - min(values)) / max(abs(float(np.mean(values))), 1e-9)


def _line_slope(points: list[tuple[int, float]]) -> float:
    if len(points) < 2:
        return 0.0
    x = np.asarray([p[0] for p in points], dtype=np.float64)
    y = np.asarray([p[1] for p in points], dtype=np.float64)
    return float(np.polyfit(x, y, 1)[0] / max(abs(float(np.mean(y))), 1e-9))


def detect_chart_patterns(
    candles: CandleSeries,
    structure: StructureState,
    atr: float,
    tolerance_atr: float = 0.6,
) -> tuple[PatternDetection, ...]:
    swings = structure.swings[-14:]
    highs = [s for s in swings if s.kind is SwingKind.HIGH]
    lows = [s for s in swings if s.kind is SwingKind.LOW]
    patterns: list[PatternDetection] = []
    tolerance = max(atr * tolerance_atr / max(candles.latest_close, 1e-9), 0.003)
    end = len(candles) - 1

    if len(highs) >= 2 and len(lows) >= 2:
        high_points = [(s.index, s.price) for s in highs[-3:]]
        low_points = [(s.index, s.price) for s in lows[-3:]]
        high_slope, low_slope = _line_slope(high_points), _line_slope(low_points)
        high_flat = _relative_spread([p[1] for p in high_points]) <= tolerance
        low_flat = _relative_spread([p[1] for p in low_points]) <= tolerance
        start = min(high_points[0][0], low_points[0][0])
        if high_flat and low_slope > 0.0002:
            patterns.append(PatternDetection("Ascending Triangle", Direction.LONG, 0.78, start, end, float(np.mean([p[1] for p in high_points]))))
        if low_flat and high_slope < -0.0002:
            patterns.append(PatternDetection("Descending Triangle", Direction.SHORT, 0.78, start, end, float(np.mean([p[1] for p in low_points]))))
        if high_slope < -0.0001 and low_slope > 0.0001:
            patterns.append(PatternDetection("Symmetrical Triangle", None, 0.72, start, end))
        if high_flat and low_flat:
            patterns.append(PatternDetection("Rectangle / consolidation", None, 0.74, start, end))
        if high_slope > 0 and low_slope > 0:
            converging = high_slope < low_slope
            patterns.append(PatternDetection("Rising Wedge", Direction.SHORT, 0.7 if converging else 0.6, start, end))
        if high_slope < 0 and low_slope < 0:
            converging = abs(high_slope) > abs(low_slope)
            patterns.append(PatternDetection("Falling Wedge", Direction.LONG, 0.7 if converging else 0.6, start, end))

    if len(highs) >= 2:
        first, second = highs[-2:]
        spread = abs(second.price - first.price) / max(atr, 1e-9)
        troughs = [s for s in lows if first.index < s.index < second.index]
        if spread <= tolerance_atr and troughs:
            quality = min(0.95, 0.7 + 0.1 * (1 - spread / max(tolerance_atr, 1e-9)))
            patterns.append(PatternDetection("Double Top", Direction.SHORT, quality, first.index, end, min(t.price for t in troughs)))
    if len(lows) >= 2:
        first, second = lows[-2:]
        spread = abs(second.price - first.price) / max(atr, 1e-9)
        peaks = [s for s in highs if first.index < s.index < second.index]
        if spread <= tolerance_atr and peaks:
            quality = min(0.95, 0.7 + 0.1 * (1 - spread / max(tolerance_atr, 1e-9)))
            patterns.append(PatternDetection("Double Bottom", Direction.LONG, quality, first.index, end, max(p.price for p in peaks)))
    if len(highs) >= 3 and _relative_spread([s.price for s in highs[-3:]]) <= tolerance:
        patterns.append(PatternDetection("Triple Top", Direction.SHORT, 0.82, highs[-3].index, end))
    if len(lows) >= 3 and _relative_spread([s.price for s in lows[-3:]]) <= tolerance:
        patterns.append(PatternDetection("Triple Bottom", Direction.LONG, 0.82, lows[-3].index, end))
    if len(highs) >= 3 and len(lows) >= 2:
        a, head, c = highs[-3:]
        shoulders_close = abs(a.price - c.price) <= atr * tolerance_atr
        if head.price > max(a.price, c.price) + atr * 0.4 and shoulders_close:
            patterns.append(PatternDetection("Head and Shoulders", Direction.SHORT, 0.84, a.index, end, float(np.mean([s.price for s in lows[-2:]]))))
    if len(lows) >= 3 and len(highs) >= 2:
        a, head, c = lows[-3:]
        shoulders_close = abs(a.price - c.price) <= atr * tolerance_atr
        if head.price < min(a.price, c.price) - atr * 0.4 and shoulders_close:
            patterns.append(PatternDetection("Inverse Head and Shoulders", Direction.LONG, 0.84, a.index, end, float(np.mean([s.price for s in highs[-2:]]))))

    # Lightweight flag/pennant: strong 20-bar impulse then shallow 8-bar counter-channel/contraction.
    if len(candles) >= 30:
        impulse = float(candles.close[-9] - candles.close[-29])
        consolidation = float(candles.close[-1] - candles.close[-9])
        impulse_atr = abs(impulse) / max(atr, 1e-9)
        recent_range = float(np.max(candles.high[-9:]) - np.min(candles.low[-9:]))
        prior_range = float(np.max(candles.high[-18:-9]) - np.min(candles.low[-18:-9]))
        if impulse_atr >= 2.5 and abs(consolidation) <= abs(impulse) * 0.45:
            direction = Direction.LONG if impulse > 0 else Direction.SHORT
            counter = consolidation * impulse <= 0
            contracted = recent_range < prior_range * 0.8
            name = ("Bull" if direction is Direction.LONG else "Bear") + (" Pennant" if contracted else " Flag")
            quality = 0.72 + 0.08 * counter + 0.08 * contracted
            patterns.append(PatternDetection(name, direction, quality, len(candles) - 29, end))
    return tuple(pattern for pattern in patterns if pattern.quality >= 0.65)
