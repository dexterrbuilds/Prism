from __future__ import annotations

from dataclasses import dataclass

from app.analysis.context import AnalysisContext
from app.models import (
    Direction,
    EntryQuality,
    MarketRegime,
    RejectionReason,
    SetupCandidate,
    SignalGrade,
    SignalMode,
    TradePlan,
    VolatilityClass,
)
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
    entry_quality: EntryQuality | None = None,
    minimum_entry_score: int = 75,
) -> ValidationResult:
    primary_tf = "15m" if candidate.mode is SignalMode.SCALP else "1h"
    execution_tf = "5m" if candidate.mode is SignalMode.SCALP else "15m"
    primary = context.timeframes[primary_tf]
    price = context.snapshot.series[execution_tf].latest_close
    atr = float(primary.indicators.atr[-1])
    if not trade_geometry_valid(plan, candidate.direction):
        return ValidationResult(False, RejectionReason.POOR_RR)
    if entry_quality is not None and entry_quality.hard_reasons:
        reason = RejectionReason.ENTRY_TOO_LATE if "ENTRY_TOO_LATE" in entry_quality.hard_reasons else RejectionReason.ENTRY_QUALITY
        return ValidationResult(False, reason)
    if score.total < 70:
        return ValidationResult(False, RejectionReason.LOW_CONFLUENCE)
    if score.total < minimum_valid_score and score.grade is not SignalGrade.WATCH:
        return ValidationResult(False, RejectionReason.LOW_CONFLUENCE)
    if entry_quality is None and not candidate.confirmed and score.grade in {SignalGrade.VALID, SignalGrade.EXCEPTIONAL}:
        return ValidationResult(False, RejectionReason.NO_SETUP)
    if entry_quality is None and is_entry_too_late(price, candidate, atr, max_chase_atr):
        return ValidationResult(False, RejectionReason.ENTRY_TOO_LATE)
    if context.regime is MarketRegime.UNCLEAR and score.total < 84:
        return ValidationResult(False, RejectionReason.UNCLEAR_REGIME)
    opposing = {MarketRegime.STRONG_BEARISH_TREND, MarketRegime.BEARISH_TREND} if candidate.direction is Direction.LONG else {MarketRegime.STRONG_BULLISH_TREND, MarketRegime.BULLISH_TREND}
    reversal = any(token in candidate.strategy for token in ("REVERSAL", "FAILED", "SWEEP", "HEAD_AND_SHOULDERS"))
    if context.regime in opposing and not reversal:
        return ValidationResult(False, RejectionReason.HTF_CONFLICT)
    if primary.volatility is VolatilityClass.HIGH and plan.stop_distance_atr > 3.5:
        return ValidationResult(False, RejectionReason.EXCESSIVE_VOLATILITY)
    if candidate.mode is SignalMode.SCALP:
        range_family = any(token in candidate.strategy for token in ("RANGE", "REJECTION", "SWEEP"))
        if primary.volatility is VolatilityClass.LOW and not range_family:
            return ValidationResult(False, RejectionReason.INSUFFICIENT_VOLATILITY)
        if primary.volume.relative_volume < 0.6:
            return ValidationResult(False, RejectionReason.INSUFFICIENT_VOLUME)
    if candidate.metadata.get("require_volume") and not primary.volume.breakout_confirmed:
        return ValidationResult(False, RejectionReason.INSUFFICIENT_VOLUME)
    if not room_to_target(plan, primary.zones, candidate.direction):
        return ValidationResult(False, RejectionReason.INSUFFICIENT_ROOM_TO_TARGET)
    if plan.reward_risk < 2.0:
        return ValidationResult(False, RejectionReason.POOR_RR)
    # Entry quality below the activation threshold is a patient WAIT, not a
    # rejected directional thesis. The scanner persists it as a forming setup.
    if entry_quality is not None and entry_quality.total < minimum_entry_score:
        return ValidationResult(True, None)
    return ValidationResult(True, None)
