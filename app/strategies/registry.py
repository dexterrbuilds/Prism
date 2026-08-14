from __future__ import annotations

from app.analysis.context import AnalysisContext
from app.models import SetupCandidate
from app.strategies.base import SetupDetector
from app.strategies.breakout import BreakoutSetupDetector
from app.strategies.liquidity import LiquiditySetupDetector
from app.strategies.reversal import ReversalSetupDetector
from app.strategies.trend import TrendSetupDetector

DETECTORS: tuple[SetupDetector, ...] = (
    TrendSetupDetector(),
    BreakoutSetupDetector(),
    ReversalSetupDetector(),
    LiquiditySetupDetector(),
)


def detect_setups(context: AnalysisContext) -> list[SetupCandidate]:
    candidates: list[SetupCandidate] = []
    for detector in DETECTORS:
        candidates.extend(detector.detect(context))
    return candidates
