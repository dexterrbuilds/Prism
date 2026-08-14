from __future__ import annotations

from dataclasses import dataclass

import talib

from app.models import CandleSeries, Direction


@dataclass(frozen=True, slots=True)
class CandlestickDetection:
    name: str
    direction: Direction | None
    strength: int
    index: int


PATTERNS = {
    "Engulfing": talib.CDLENGULFING,
    "Hammer": talib.CDLHAMMER,
    "Hanging Man": talib.CDLHANGINGMAN,
    "Inverted Hammer": talib.CDLINVERTEDHAMMER,
    "Shooting Star": talib.CDLSHOOTINGSTAR,
    "Morning Star": talib.CDLMORNINGSTAR,
    "Evening Star": talib.CDLEVENINGSTAR,
    "Doji": talib.CDLDOJI,
    "Dragonfly Doji": talib.CDLDRAGONFLYDOJI,
    "Gravestone Doji": talib.CDLGRAVESTONEDOJI,
    "Harami": talib.CDLHARAMI,
    "Harami Cross": talib.CDLHARAMICROSS,
    "Three White Soldiers": talib.CDL3WHITESOLDIERS,
    "Three Black Crows": talib.CDL3BLACKCROWS,
    "Piercing Pattern": talib.CDLPIERCING,
    "Dark Cloud Cover": talib.CDLDARKCLOUDCOVER,
}


def detect_candlesticks(candles: CandleSeries, lookback: int = 3) -> tuple[CandlestickDetection, ...]:
    found: list[CandlestickDetection] = []
    for name, function in PATTERNS.items():
        values = function(candles.open, candles.high, candles.low, candles.close)
        for index in range(max(0, len(values) - lookback), len(values)):
            value = int(values[index])
            if value:
                direction: Direction | None = Direction.LONG if value > 0 else Direction.SHORT
                if name == "Doji":
                    direction = None
                found.append(CandlestickDetection(name, direction, abs(value), index))
    return tuple(found)
