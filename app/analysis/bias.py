from __future__ import annotations

from app.analysis.context import AnalysisContext
from app.models import Direction, DirectionalBias, MarketRegime, StructureBias


def derive_directional_bias(context: AnalysisContext) -> DirectionalBias:
    macro_map = {
        MarketRegime.STRONG_BULLISH_TREND: (Direction.LONG, 1.0),
        MarketRegime.BULLISH_TREND: (Direction.LONG, 0.82),
        MarketRegime.STRONG_BEARISH_TREND: (Direction.SHORT, 1.0),
        MarketRegime.BEARISH_TREND: (Direction.SHORT, 0.82),
    }
    direction, strength = macro_map.get(context.regime, (None, 0.35))
    primary_bias = context.timeframes["1h"].structure.bias
    structure_direction = (
        Direction.LONG
        if primary_bias is StructureBias.BULLISH
        else Direction.SHORT
        if primary_bias is StructureBias.BEARISH
        else None
    )
    evidence = [f"4H regime is {context.regime.value.lower().replace('_', ' ')}"]
    if direction is not None and structure_direction is direction:
        strength = min(1.0, strength + 0.1)
        evidence.append("1H structure aligns with the macro direction")
    elif direction is None and structure_direction is not None:
        direction = structure_direction
        strength = 0.55
        evidence.append("1H structure supplies the only directional bias")
    elif direction is not None and structure_direction is not None and structure_direction is not direction:
        strength = max(0.2, strength - 0.35)
        evidence.append("1H structure conflicts with the macro direction")
    return DirectionalBias(direction, strength, "4h/1h", tuple(evidence))
