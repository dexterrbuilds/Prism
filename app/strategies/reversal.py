from __future__ import annotations

from app.analysis.context import AnalysisContext
from app.models import ConfluenceEvidence, Direction, MarketRegime, SetupCandidate, ZoneKind
from app.strategies.common import candidate, confirmation, zone_evidence


class ReversalSetupDetector:
    def detect(self, context: AnalysisContext) -> list[SetupCandidate]:
        primary = context.timeframes["1h"]
        candles = context.snapshot.series["1h"]
        price = candles.latest_close
        atr = float(primary.indicators.atr[-1])
        results: list[SetupCandidate] = []
        for zone in primary.zones:
            near = zone.low - atr * 0.2 <= price <= zone.high + atr * 0.2
            if not near or zone.score < 3.0:
                continue
            if zone.kind in {ZoneKind.SUPPORT, ZoneKind.MIXED} and candles.close[-1] > candles.open[-1]:
                direction, name = Direction.LONG, "SUPPORT_BOUNCE"
                if context.regime is MarketRegime.RANGE:
                    name = "RANGE_LOW_REVERSAL"
                invalidation = zone.low - atr * 0.45
            elif zone.kind in {ZoneKind.RESISTANCE, ZoneKind.MIXED} and candles.close[-1] < candles.open[-1]:
                direction, name = Direction.SHORT, "RESISTANCE_REJECTION"
                if context.regime is MarketRegime.RANGE:
                    name = "RANGE_HIGH_REVERSAL"
                invalidation = zone.high + atr * 0.45
            else:
                continue
            confirmed, lower_evidence = confirmation(context, direction)
            ev = ConfluenceEvidence(location=zone_evidence(zone), structure=[f"1H {name.lower().replace('_', ' ')}"], candlestick=lower_evidence)
            results.append(candidate(context, name, direction, zone.low, zone.high, invalidation, "15M rejection from zone", 0.7 + min(0.2, zone.score / 50), ev, confirmed))
        for event in primary.structure.events:
            if event.name != "CHOCH":
                continue
            direction = event.direction
            confirmed, lower_evidence = confirmation(context, direction)
            event_invalidation = primary.structure.significant_low if direction is Direction.LONG else primary.structure.significant_high
            if event_invalidation is None:
                continue
            ev = ConfluenceEvidence(structure=[f"1H CHoCH through {event.level:.8g}"], momentum=list(primary.momentum.evidence), candlestick=lower_evidence, pattern=["Confirmed character change"])
            results.append(candidate(context, "CHOCH_REVERSAL", direction, event.level - atr * 0.15, event.level + atr * 0.15, event_invalidation, "15M holds CHoCH level", 0.84, ev, confirmed))
        for divergence in primary.divergences:
            if divergence.quality < 0.65:
                continue
            direction = divergence.direction
            confirmed, lower_evidence = confirmation(context, direction)
            divergence_invalidation = primary.structure.significant_low if direction is Direction.LONG else primary.structure.significant_high
            if divergence_invalidation is None:
                continue
            ev = ConfluenceEvidence(momentum=[f"{divergence.name.title()} {divergence.indicator} divergence on confirmed pivots"], structure=["Divergence located at 1H swing"], candlestick=lower_evidence)
            results.append(candidate(context, "DIVERGENCE_REVERSAL", direction, price - atr * 0.2, price + atr * 0.2, divergence_invalidation, "15M reversal confirmation after divergence", divergence.quality, ev, confirmed))
        lower_band, upper_band = float(primary.indicators.bb_lower[-1]), float(primary.indicators.bb_upper[-1])
        if context.regime is MarketRegime.RANGE:
            for direction, breached in ((Direction.LONG, candles.low[-1] < lower_band and price > lower_band), (Direction.SHORT, candles.high[-1] > upper_band and price < upper_band)):
                if not breached:
                    continue
                confirmed, lower_evidence = confirmation(context, direction)
                invalidation = float(candles.low[-1] - atr * 0.25) if direction is Direction.LONG else float(candles.high[-1] + atr * 0.25)
                ev = ConfluenceEvidence(volatility=["Bollinger band excursion closed back inside"], location=["Range extreme mean-reversion location"], candlestick=lower_evidence)
                results.append(candidate(context, "BOLLINGER_MEAN_REVERSION", direction, price - atr * 0.15, price + atr * 0.15, invalidation, "Close remains inside Bollinger band", 0.75, ev, confirmed))
        mapping = {
            "Double Bottom": "DOUBLE_BOTTOM_REVERSAL", "Double Top": "DOUBLE_TOP_REVERSAL",
            "Triple Bottom": "DOUBLE_BOTTOM_REVERSAL", "Triple Top": "DOUBLE_TOP_REVERSAL",
            "Head and Shoulders": "HEAD_AND_SHOULDERS", "Inverse Head and Shoulders": "INVERSE_HEAD_AND_SHOULDERS",
        }
        for pattern in primary.patterns:
            strategy = mapping.get(pattern.name)
            if not strategy or pattern.direction is None:
                continue
            direction = pattern.direction
            level = pattern.breakout_level or price
            broke = price > level if direction is Direction.LONG else price < level
            if not broke:
                continue
            confirmed, lower_evidence = confirmation(context, direction)
            pattern_invalidation = primary.structure.significant_low if direction is Direction.LONG else primary.structure.significant_high
            if pattern_invalidation is None:
                continue
            ev = ConfluenceEvidence(pattern=[f"{pattern.name} quality {pattern.quality:.2f}"], structure=["Pattern neckline confirmed broken"], candlestick=lower_evidence)
            results.append(candidate(context, strategy, direction, level - atr * 0.15, level + atr * 0.15, pattern_invalidation, "Neckline close and 15M hold", pattern.quality, ev, confirmed))
        return results
