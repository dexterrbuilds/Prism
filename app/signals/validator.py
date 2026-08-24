from __future__ import annotations

from dataclasses import dataclass

from app.analysis.context import AnalysisContext
from app.models import Direction, MarketRegime, RejectionReason, SetupCandidate, SignalGrade, TradePlan, VolatilityClass
from app.signals.risk import is_entry_too_late, room_to_target, trade_geometry_valid
from app.signals.scoring import ScoreResult


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    reason: RejectionReason | None


def validate_candidate(
    candidate: SetupCandidate,
    context: AnalysisContext,
    score: ScoreResult,
    plan: TradePlan,
    max_chase_atr: float = 0.75,
    minimum_valid_score: int = 80,
) -> ValidationResult:
    primary = context.timeframes["1h"]
    price = context.snapshot.series["1h"].latest_close
    atr = float(primary.indicators.atr[-1])
    if not trade_geometry_valid(plan, candidate.direction):
        return ValidationResult(False, RejectionReason.POOR_RR)
    if score.total < 70:
        return ValidationResult(False, RejectionReason.LOW_CONFLUENCE)
    if score.total < minimum_valid_score and score.grade is not SignalGrade.WATCH:
        return ValidationResult(False, RejectionReason.LOW_CONFLUENCE)
    if not candidate.confirmed and score.grade in {SignalGrade.VALID, SignalGrade.EXCEPTIONAL}:
        return ValidationResult(False, RejectionReason.NO_SETUP)
    if is_entry_too_late(price, candidate, atr, max_chase_atr):
        return ValidationResult(False, RejectionReason.ENTRY_TOO_LATE)
    if context.regime is MarketRegime.UNCLEAR and score.total < 84:
        return ValidationResult(False, RejectionReason.UNCLEAR_REGIME)
    opposing = {MarketRegime.STRONG_BEARISH_TREND, MarketRegime.BEARISH_TREND} if candidate.direction is Direction.LONG else {MarketRegime.STRONG_BULLISH_TREND, MarketRegime.BULLISH_TREND}
    reversal = any(token in candidate.strategy for token in ("REVERSAL", "FAILED", "SWEEP", "HEAD_AND_SHOULDERS"))
    if context.regime in opposing and not reversal:
        return ValidationResult(False, RejectionReason.HTF_CONFLICT)
    if primary.volatility is VolatilityClass.HIGH and plan.stop_distance_atr > 3.5:
        return ValidationResult(False, RejectionReason.EXCESSIVE_VOLATILITY)
    if candidate.metadata.get("require_volume") and not primary.volume.breakout_confirmed:
        return ValidationResult(False, RejectionReason.INSUFFICIENT_VOLUME)
    if not room_to_target(plan, primary.zones, candidate.direction):
        return ValidationResult(False, RejectionReason.INSUFFICIENT_ROOM_TO_TARGET)
    if plan.reward_risk < 2.0:
        return ValidationResult(False, RejectionReason.POOR_RR)
    return ValidationResult(True, None)
