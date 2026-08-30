from __future__ import annotations

from app.analysis.context import AnalysisContext
from app.models import ConfluenceEvidence, Direction, MarketRegime, SetupCandidate, SignalMode, StructureBias, ZoneKind
from app.strategies.common import candidate


def _confirmed(context: AnalysisContext, direction: Direction) -> tuple[bool, list[str]]:
    execution = context.timeframes["5m"]
    evidence: list[str] = []
    event = next((event for event in reversed(execution.structure.events) if event.direction is direction), None)
    if event is not None:
        evidence.append(f"5M {event.name} confirmation")
    candle = next(
        (
            item
            for item in reversed(execution.candlesticks)
            if item.direction is direction and item.index == len(context.snapshot.series["5m"]) - 1
        ),
        None,
    )
    if candle is not None:
        evidence.append(f"5M {candle.name.lower()} at setup location")
    return bool(evidence), evidence


def _add(
    context: AnalysisContext,
    results: list[SetupCandidate],
    name: str,
    direction: Direction,
    low: float,
    high: float,
    invalidation: float,
    trigger: str,
    quality: float,
    evidence: ConfluenceEvidence,
) -> None:
    confirmed, lower = _confirmed(context, direction)
    evidence.candlestick.extend(lower)
    results.append(
        candidate(
            context,
            name,
            direction,
            low,
            high,
            invalidation,
            trigger,
            quality,
            evidence,
            confirmed,
            timeframe="15m",
            mode=SignalMode.SCALP,
        )
    )


class ScalpSetupDetector:
    """5M execution setups routed by 1H bias and 15M local structure."""

    def detect(self, context: AnalysisContext) -> list[SetupCandidate]:
        if "5m" not in context.timeframes:
            return []
        local = context.timeframes["15m"]
        execution = context.timeframes["5m"]
        candles = context.snapshot.series["5m"]
        price = candles.latest_close
        previous = float(candles.close[-2])
        atr = float(execution.indicators.atr[-1])
        vwap = float(execution.indicators.vwap[-1])
        results: list[SetupCandidate] = []
        bullish_context = context.regime in {MarketRegime.STRONG_BULLISH_TREND, MarketRegime.BULLISH_TREND} or local.structure.bias is StructureBias.BULLISH
        bearish_context = context.regime in {MarketRegime.STRONG_BEARISH_TREND, MarketRegime.BEARISH_TREND} or local.structure.bias is StructureBias.BEARISH

        for liquidity_event in execution.liquidity:
            invalidation = (
                min(float(candles.low[-1]), liquidity_event.level) - atr * 0.2
                if liquidity_event.direction is Direction.LONG
                else max(float(candles.high[-1]), liquidity_event.level) + atr * 0.2
            )
            name = "SCALP_LIQUIDITY_SWEEP_RECLAIM"
            if "failed breakout" in liquidity_event.name:
                name = "SCALP_FAILED_BREAKOUT"
            elif "failed breakdown" in liquidity_event.name:
                name = "SCALP_FAILED_BREAKDOWN"
            _add(
                context,
                results,
                name,
                liquidity_event.direction,
                liquidity_event.level - atr * 0.12,
                liquidity_event.level + atr * 0.12,
                invalidation,
                "5M sweep, reclaim, and closed structure confirmation",
                liquidity_event.quality,
                ConfluenceEvidence(structure=list(liquidity_event.evidence), location=[f"5M liquidity level {liquidity_event.level:.8g}"], pattern=[liquidity_event.name]),
            )

        boundaries = ((local.structure.range_high, Direction.LONG), (local.structure.range_low, Direction.SHORT))
        for level, direction in boundaries:
            if level is None:
                continue
            broke = previous <= level < price if direction is Direction.LONG else previous >= level > price
            retested = (
                candles.low[-1] <= level <= price if direction is Direction.LONG else candles.high[-1] >= level >= price
            ) and abs(price - level) <= atr * 0.5
            invalidation = level - atr * 0.55 if direction is Direction.LONG else level + atr * 0.55
            if broke or retested:
                name = "SCALP_BREAKOUT_RETEST" if retested else "SCALP_VOLATILITY_EXPANSION"
                _add(
                    context,
                    results,
                    name,
                    direction,
                    level - atr * 0.12,
                    level + atr * 0.12,
                    invalidation,
                    "5M breakout level retests and closes in the breakout direction",
                    0.82 if retested else 0.72,
                    ConfluenceEvidence(structure=["15M range boundary broken"], location=[f"Retest level {level:.8g}"], volume=list(execution.volume.evidence)),
                )

        for zone in local.zones:
            near = zone.low - atr * 0.25 <= price <= zone.high + atr * 0.25
            if not near or zone.score < 3:
                continue
            if zone.kind in {ZoneKind.SUPPORT, ZoneKind.MIXED} and price > float(candles.open[-1]):
                direction = Direction.LONG
                names = ["SCALP_SUPPORT_REJECTION"]
                if local.structure.bias is StructureBias.RANGE:
                    names.append("SCALP_RANGE_LOW_LONG")
                invalidation = zone.low - atr * 0.35
            elif zone.kind in {ZoneKind.RESISTANCE, ZoneKind.MIXED} and price < float(candles.open[-1]):
                direction = Direction.SHORT
                names = ["SCALP_RESISTANCE_REJECTION"]
                if local.structure.bias is StructureBias.RANGE:
                    names.append("SCALP_RANGE_HIGH_SHORT")
                invalidation = zone.high + atr * 0.35
            else:
                continue
            for name in names:
                _add(context, results, name, direction, zone.low, zone.high, invalidation, "5M rejection closes away from the 15M zone", 0.76, ConfluenceEvidence(location=["15M scored reaction zone"], structure=["5M rejection response"]))

        ema20 = float(execution.indicators.ema20[-1])
        for direction, aligned in ((Direction.LONG, bullish_context), (Direction.SHORT, bearish_context)):
            if not aligned:
                continue
            directional_close = price > float(candles.open[-1]) if direction is Direction.LONG else price < float(candles.open[-1])
            if abs(price - ema20) <= atr * 0.35 and directional_close:
                invalidation = ema20 - atr * 0.6 if direction is Direction.LONG else ema20 + atr * 0.6
                _add(context, results, "SCALP_EMA_PULLBACK", direction, ema20 - atr * 0.15, ema20 + atr * 0.15, invalidation, "5M EMA20 pullback rejects with structure confirmation", 0.74, ConfluenceEvidence(location=["5M EMA20 pullback area"], trend=["1H directional context aligns"]))

        if previous <= vwap < price:
            _add(context, results, "SCALP_VWAP_RECLAIM", Direction.LONG, vwap - atr * 0.12, vwap + atr * 0.12, vwap - atr * 0.55, "5M closes above VWAP after reclaim", 0.76, ConfluenceEvidence(location=["Session VWAP reclaim"], trend=["VWAP used as execution location"]))
        elif previous >= vwap > price:
            _add(context, results, "SCALP_VWAP_REJECTION", Direction.SHORT, vwap - atr * 0.12, vwap + atr * 0.12, vwap + atr * 0.55, "5M closes below VWAP after rejection", 0.76, ConfluenceEvidence(location=["Session VWAP rejection"], trend=["VWAP used as execution location"]))

        for structure_event in local.structure.events:
            structure_invalidation = local.structure.significant_low if structure_event.direction is Direction.LONG else local.structure.significant_high
            if structure_invalidation is None:
                continue
            name = "SCALP_CHOCH_REVERSAL" if structure_event.name == "CHOCH" else "SCALP_BOS_CONTINUATION"
            _add(context, results, name, structure_event.direction, structure_event.level - atr * 0.15, structure_event.level + atr * 0.15, structure_invalidation, f"5M confirms the 15M {structure_event.name}", 0.8, ConfluenceEvidence(structure=[f"15M {structure_event.name} at {structure_event.level:.8g}"]))

        for direction, aligned in ((Direction.LONG, bullish_context), (Direction.SHORT, bearish_context)):
            if not aligned or execution.momentum.direction is not direction:
                continue
            name = "SCALP_MOMENTUM_CONTINUATION"
            invalidation = price - atr * 0.8 if direction is Direction.LONG else price + atr * 0.8
            _add(context, results, name, direction, price - atr * 0.2, price + atr * 0.2, invalidation, "5M momentum pullback confirms without chase", 0.7, ConfluenceEvidence(momentum=list(execution.momentum.evidence), trend=["1H context aligns"]))

        return results
