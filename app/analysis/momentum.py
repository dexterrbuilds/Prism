from __future__ import annotations

from dataclasses import dataclass

from app.analysis.indicators import IndicatorSet
from app.models import Direction


@dataclass(frozen=True, slots=True)
class MomentumState:
    direction: Direction | None
    strength: float
    evidence: tuple[str, ...]
    overextended: bool


def evaluate_momentum(indicators: IndicatorSet) -> MomentumState:
    evidence: list[str] = []
    bull = bear = 0.0
    rsi = indicators.rsi
    hist = indicators.macd_hist
    if rsi[-2] <= 50 < rsi[-1] or (rsi[-3] < rsi[-2] < rsi[-1] and rsi[-1] < 70):
        bull += 1.0
        evidence.append("RSI momentum recovering")
    if rsi[-2] >= 50 > rsi[-1] or (rsi[-3] > rsi[-2] > rsi[-1] and rsi[-1] > 30):
        bear += 1.0
        evidence.append("RSI momentum deteriorating")
    if hist[-2] <= 0 < hist[-1] or hist[-1] > hist[-2] > hist[-3]:
        bull += 1.0
        evidence.append("MACD histogram expanding bullish")
    if hist[-2] >= 0 > hist[-1] or hist[-1] < hist[-2] < hist[-3]:
        bear += 1.0
        evidence.append("MACD histogram expanding bearish")
    if indicators.roc[-1] > 0 and indicators.cci[-1] > 0:
        bull += 0.5
    elif indicators.roc[-1] < 0 and indicators.cci[-1] < 0:
        bear += 0.5
    direction = Direction.LONG if bull > bear + 0.5 else Direction.SHORT if bear > bull + 0.5 else None
    overextended = bool(rsi[-1] > 78 or rsi[-1] < 22 or abs(indicators.cci[-1]) > 220)
    return MomentumState(direction, min(1.0, max(bull, bear) / 2.5), tuple(evidence), overextended)
