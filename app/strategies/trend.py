from __future__ import annotations

from app.analysis.context import AnalysisContext
from app.models import ConfluenceEvidence, Direction, MarketRegime, SetupCandidate, StructureBias
from app.strategies.common import candidate, confirmation


class TrendSetupDetector:
    def detect(self, context: AnalysisContext) -> list[SetupCandidate]:
        primary = context.timeframes["1h"]
        candles = context.snapshot.series["1h"]
        indicators = primary.indicators
        atr = float(indicators.atr[-1])
        price = candles.latest_close
        bullish_regimes = {MarketRegime.STRONG_BULLISH_TREND, MarketRegime.BULLISH_TREND}
        bearish_regimes = {MarketRegime.STRONG_BEARISH_TREND, MarketRegime.BEARISH_TREND}
        direction = Direction.LONG if context.regime in bullish_regimes else Direction.SHORT if context.regime in bearish_regimes else None
        if direction is None:
            return []
        sign = 1 if direction is Direction.LONG else -1
        aligned = primary.structure.bias is (StructureBias.BULLISH if direction is Direction.LONG else StructureBias.BEARISH)
        confirmed, confirmation_evidence = confirmation(context, direction)
        results: list[SetupCandidate] = []
        trend_text = "4H bullish EMA structure" if direction is Direction.LONG else "4H bearish EMA structure"
        structure_text = "1H bullish structure" if direction is Direction.LONG else "1H bearish structure"
        ema20, ema50 = float(indicators.ema20[-1]), float(indicators.ema50[-1])
        pullback_level = ema20 if abs(price - ema20) < abs(price - ema50) else ema50
        near_ema = abs(price - pullback_level) <= atr * 0.65
        retraced = sign * (price - float(indicators.ema20[-5])) < atr * 1.0
        if aligned and near_ema and retraced:
            ev = ConfluenceEvidence(
                trend=[trend_text], structure=[structure_text], location=["1H EMA pullback zone"],
                momentum=list(primary.momentum.evidence[:1]), higher_timeframe=[f"4H {context.regime.value.lower().replace('_', ' ')}"],
            )
            ev.candlestick.extend(confirmation_evidence)
            invalidation = (primary.structure.significant_low if direction is Direction.LONG else primary.structure.significant_high)
            if invalidation is not None:
                for strategy in ("TREND_PULLBACK", "EMA_PULLBACK"):
                    results.append(candidate(context, strategy, direction, pullback_level - atr * 0.2, pullback_level + atr * 0.2, invalidation, f"15M rejection and close {'above' if direction is Direction.LONG else 'below'} EMA zone", 0.78, ev, confirmed))
        matching_events = [event for event in primary.structure.events if event.direction is direction and event.name == "BOS"]
        if aligned and matching_events and primary.volume.breakout_confirmed:
            event = matching_events[-1]
            ev = ConfluenceEvidence(
                trend=[trend_text], structure=[f"1H BOS through {event.level:.8g}"],
                momentum=list(primary.momentum.evidence[:1]), volume=list(primary.volume.evidence) or ["1H relative volume confirms continuation"],
                higher_timeframe=[trend_text],
            )
            ev.candlestick.extend(confirmation_evidence)
            invalidation = primary.structure.significant_low if direction is Direction.LONG else primary.structure.significant_high
            if invalidation is not None:
                results.append(candidate(context, "BOS_CONTINUATION", direction, event.level - atr * 0.15, event.level + atr * 0.25, invalidation, "BOS level holds on 15M", 0.84, ev, confirmed))
        if aligned and primary.momentum.direction is direction and primary.volume.direction is direction and not primary.momentum.overextended:
            ev = ConfluenceEvidence(trend=[trend_text], structure=[structure_text], momentum=list(primary.momentum.evidence), volume=list(primary.volume.evidence), higher_timeframe=[trend_text])
            invalidation = primary.structure.significant_low if direction is Direction.LONG else primary.structure.significant_high
            if invalidation is not None:
                results.append(candidate(context, "MOMENTUM_CONTINUATION", direction, price - atr * 0.2, price + atr * 0.1, invalidation, "15M momentum remains aligned after shallow pause", 0.72, ev, confirmed))
        return results
