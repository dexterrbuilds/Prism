from __future__ import annotations

import math
import re

from app.models import SetupCandidate, TradePlan

_TIMEFRAME = re.compile(r"^(\d+)([mhdw])$")
_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1_440, "w": 10_080}


def timeframe_minutes(timeframe: str) -> int:
    match = _TIMEFRAME.fullmatch(timeframe.strip().lower())
    if match is None:
        raise ValueError(f"unsupported setup timeframe: {timeframe}")
    return int(match.group(1)) * _UNIT_MINUTES[match.group(2)]


def derive_setup_validity_minutes(candidate: SetupCandidate, trade: TradePlan) -> int:
    """Derive setup validity from its own timeframe and projected trade horizon."""
    analysis_bar_minutes = timeframe_minutes(candidate.timeframe)
    configured_bars = candidate.metadata.get("validity_bars")
    if configured_bars is not None:
        try:
            validity_bars = float(configured_bars)
        except (TypeError, ValueError) as exc:
            raise ValueError("validity_bars must be numeric") from exc
        if not math.isfinite(validity_bars) or validity_bars <= 0:
            raise ValueError("validity_bars must be positive")
        return max(analysis_bar_minutes, math.ceil(validity_bars) * analysis_bar_minutes)

    # The risk engine's lower hold estimate is strategy-aware and target-geometry
    # aware. Using it as the setup horizon makes a breakout, pullback, reversal,
    # or wider target expire on its own cadence rather than a global clock.
    projected_hours = trade.estimated_hold_hours_low
    if projected_hours is not None and math.isfinite(projected_hours) and projected_hours > 0:
        projected_minutes = projected_hours * 60
    else:
        target_r = abs(trade.tp2 - trade.preferred_entry) / max(trade.risk_per_unit, 1e-12)
        projected_minutes = target_r * analysis_bar_minutes

    validity_bars = max(1, math.ceil(projected_minutes / analysis_bar_minutes))
    return validity_bars * analysis_bar_minutes
