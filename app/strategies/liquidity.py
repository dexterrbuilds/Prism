from __future__ import annotations

from app.analysis.context import AnalysisContext
from app.models import ConfluenceEvidence, Direction, SetupCandidate
from app.strategies.common import candidate, confirmation


class LiquiditySetupDetector:
    def detect(self, context: AnalysisContext) -> list[SetupCandidate]:
        primary = context.timeframes["1h"]
        atr = float(primary.indicators.atr[-1])
        results: list[SetupCandidate] = []
        for event in primary.liquidity:
            confirmed, lower_evidence = confirmation(context, event.direction)
            if event.direction is Direction.LONG:
                invalidation = min(float(context.snapshot.series["1h"].low[-1]), event.level) - atr * 0.25
            else:
                invalidation = max(float(context.snapshot.series["1h"].high[-1]), event.level) + atr * 0.25
            name = "LIQUIDITY_SWEEP_REVERSAL"
            if "failed breakout" in event.name:
                name = "FAILED_BREAKOUT"
            elif "failed breakdown" in event.name:
                name = "FAILED_BREAKDOWN"
            ev = ConfluenceEvidence(structure=list(event.evidence), location=[f"Liquidity event at {event.level:.8g}"], pattern=[event.name], candlestick=lower_evidence)
            results.append(candidate(context, name, event.direction, event.level - atr * 0.15, event.level + atr * 0.15, invalidation, "15M reclaim/rejection holds", event.quality, ev, confirmed))
        return results
