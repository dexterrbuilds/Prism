from __future__ import annotations

from app.analysis.context import AnalysisContext
from app.models import ConfluenceEvidence, Direction, MarketRegime, SetupCandidate, VolatilityClass
from app.strategies.common import candidate, confirmation


class BreakoutSetupDetector:
    def detect(self, context: AnalysisContext) -> list[SetupCandidate]:
        primary = context.timeframes["1h"]
        candles = context.snapshot.series["1h"]
        price, previous = candles.latest_close, float(candles.close[-2])
        atr = float(primary.indicators.atr[-1])
        results: list[SetupCandidate] = []
        boundaries = ((primary.structure.range_high, Direction.LONG), (primary.structure.range_low, Direction.SHORT))
        for level, direction in boundaries:
            if level is None:
                continue
            broke = previous <= level < price if direction is Direction.LONG else previous >= level > price
            retest = (
                candles.low[-1] <= level <= price if direction is Direction.LONG else candles.high[-1] >= level >= price
            ) and abs(price - level) <= atr * 0.55
            confirmed, lower_evidence = confirmation(context, direction)
            invalidation = level - atr * 0.65 if direction is Direction.LONG else level + atr * 0.65
            base_name = "BREAKOUT" if direction is Direction.LONG else "BREAKDOWN"
            if broke and primary.volume.breakout_confirmed:
                ev = ConfluenceEvidence(
                    structure=[f"1H close beyond range {'high' if direction is Direction.LONG else 'low'}"],
                    location=[f"Range boundary {level:.8g} broken"], volume=list(primary.volume.evidence) or ["Relative volume above breakout threshold"],
                    volatility=[f"Volatility {primary.volatility.value.lower()}"], pattern=["Confirmed range boundary break"],
                )
                ev.candlestick.extend(lower_evidence)
                results.append(candidate(context, base_name, direction, level, price, invalidation, f"Closed 1H {'above' if direction is Direction.LONG else 'below'} {level:.8g}", 0.8, ev, confirmed, require_volume=True))
                if context.regime is MarketRegime.COMPRESSION or primary.volatility is VolatilityClass.EXPANDING:
                    results.append(candidate(context, "VOLATILITY_BREAKOUT", direction, level, price, invalidation, "Compression released with range expansion", 0.84, ev, confirmed, require_volume=True))
                if context.regime is MarketRegime.RANGE:
                    results.append(candidate(context, "RANGE_BREAKOUT", direction, level, price, invalidation, "Range close and volume expansion", 0.8, ev, confirmed, require_volume=True))
            if retest and previous != price:
                held = price > level if direction is Direction.LONG else price < level
                if held:
                    ev = ConfluenceEvidence(structure=["Former range boundary reclaimed on 1H"], location=[f"Retest at {level:.8g}"], volume=list(primary.volume.evidence), pattern=["Break and retest structure"])
                    ev.candlestick.extend(lower_evidence)
                    name = "BREAKOUT_RETEST" if direction is Direction.LONG else "BREAKDOWN_RETEST"
                    results.append(candidate(context, name, direction, level - atr * 0.15, level + atr * 0.15, invalidation, "15M retest rejection close", 0.86, ev, confirmed))
        for pattern in primary.patterns:
            if pattern.direction is None:
                direction = Direction.LONG if price > float(primary.indicators.ema50[-1]) else Direction.SHORT
            else:
                direction = pattern.direction
            level = pattern.breakout_level or price
            crossed = price > level if direction is Direction.LONG else price < level
            if not crossed and pattern.name not in {"Bull Flag", "Bear Flag", "Bull Pennant", "Bear Pennant"}:
                continue
            strategy = {
                "Bull Flag": "BULL_FLAG_BREAKOUT", "Bull Pennant": "BULL_FLAG_BREAKOUT",
                "Bear Flag": "BEAR_FLAG_BREAKDOWN", "Bear Pennant": "BEAR_FLAG_BREAKDOWN",
                "Ascending Triangle": "TRIANGLE_BREAKOUT", "Descending Triangle": "TRIANGLE_BREAKOUT",
                "Symmetrical Triangle": "TRIANGLE_BREAKOUT", "Rising Wedge": "WEDGE_BREAKOUT", "Falling Wedge": "WEDGE_BREAKOUT",
            }.get(pattern.name)
            if strategy is None:
                continue
            confirmed, lower_evidence = confirmation(context, direction)
            ev = ConfluenceEvidence(pattern=[f"{pattern.name} quality {pattern.quality:.2f}"], structure=["Pattern boundary break"], volume=list(primary.volume.evidence), volatility=[f"1H volatility {primary.volatility.value.lower()}"])
            ev.candlestick.extend(lower_evidence)
            pattern_invalidation = primary.structure.significant_low if direction is Direction.LONG else primary.structure.significant_high
            if pattern_invalidation is not None:
                results.append(candidate(context, strategy, direction, level - atr * 0.15, level + atr * 0.2, pattern_invalidation, f"{pattern.name} boundary closes and holds", pattern.quality, ev, confirmed))
        # A fresh BOS/CHoCH through a swing is the deterministic trendline-break proxy in V1.
        for event in primary.structure.events:
            confirmed, lower_evidence = confirmation(context, event.direction)
            event_invalidation = primary.structure.significant_low if event.direction is Direction.LONG else primary.structure.significant_high
            if event_invalidation is None:
                continue
            ev = ConfluenceEvidence(structure=[f"1H {event.name} through pivot trend boundary"], pattern=["Swing-defined trendline break"])
            ev.candlestick.extend(lower_evidence)
            results.append(candidate(context, "TRENDLINE_BREAK", event.direction, event.level - atr * 0.15, event.level + atr * 0.2, event_invalidation, "15M close beyond swing-defined trendline", 0.7, ev, confirmed))
            if abs(price - event.level) <= atr * 0.5:
                results.append(candidate(context, "TRENDLINE_RETEST", event.direction, event.level - atr * 0.15, event.level + atr * 0.15, event_invalidation, "15M retest of broken swing trendline", 0.75, ev, confirmed))
        return results
