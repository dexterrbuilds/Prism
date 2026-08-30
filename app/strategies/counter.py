from __future__ import annotations

from app.analysis.context import AnalysisContext
from app.models import ConfluenceEvidence, Direction, SetupCandidate, Signal, SignalMode, SignalState, StructureBias
from app.strategies.common import candidate


def evaluate_counter_setup(original: Signal, context: AnalysisContext) -> SetupCandidate | None:
    """Return an opposite thesis only after independent failure/retest confirmation."""
    if original.state is not SignalState.INVALIDATED:
        return None
    direction = Direction.SHORT if original.direction is Direction.LONG else Direction.LONG
    execution_tf = "5m" if original.mode is SignalMode.SCALP else "15m"
    execution = context.timeframes[execution_tf]
    candles = context.snapshot.series[execution_tf]
    target_bias = StructureBias.BEARISH if direction is Direction.SHORT else StructureBias.BULLISH
    structure_event = next((event for event in reversed(execution.structure.events) if event.direction is direction), None)
    if execution.structure.bias is not target_bias or structure_event is None:
        return None
    failed_level = (
        original.trade.entry_zone_low if direction is Direction.SHORT else original.trade.entry_zone_high
    )
    atr = float(execution.indicators.atr[-1])
    retested = (
        candles.high[-1] >= failed_level and candles.latest_close < failed_level
        if direction is Direction.SHORT
        else candles.low[-1] <= failed_level and candles.latest_close > failed_level
    )
    if not retested:
        return None
    supporting_flow = execution.momentum.direction is direction or execution.volume.direction is direction
    if not supporting_flow:
        return None
    invalidation = failed_level + atr * 0.45 if direction is Direction.SHORT else failed_level - atr * 0.45
    name = "FAILED_BREAKOUT_SHORT" if direction is Direction.SHORT else "FAILED_BREAKDOWN_LONG"
    if original.mode is SignalMode.SCALP:
        name = f"SCALP_{name}"
    evidence = ConfluenceEvidence(
        structure=[f"{execution_tf.upper()} {structure_event.name} confirmed opposite structure"],
        location=["Failed setup level retested from the opposite side"],
        momentum=list(execution.momentum.evidence[:1]),
        volume=list(execution.volume.evidence[:1]),
        pattern=["Original thesis invalidated; counter setup qualified independently"],
    )
    return candidate(
        context,
        name,
        direction,
        failed_level - atr * 0.12,
        failed_level + atr * 0.12,
        invalidation,
        f"{execution_tf.upper()} failed-level retest closes with opposite BOS/CHoCH",
        0.86,
        evidence,
        True,
        timeframe=original.analysis_timeframe,
        mode=original.mode,
        counter_of=original.id,
    )
