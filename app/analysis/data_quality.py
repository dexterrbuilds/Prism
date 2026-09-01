from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.models import CandleSeries

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


@dataclass(frozen=True, slots=True)
class DataQualityResult:
    valid: bool
    reasons: tuple[str, ...]


def validate_candles(series: CandleSeries, minimum: int = 210) -> DataQualityResult:
    reasons: list[str] = []
    arrays = (series.timestamp, series.open, series.high, series.low, series.close, series.volume)
    lengths = {len(array) for array in arrays}
    if len(lengths) != 1 or len(series) < minimum:
        reasons.append("insufficient_or_misaligned_candles")
    if any(not np.all(np.isfinite(array)) for array in arrays[1:]):
        reasons.append("non_finite_ohlcv")
    if len(series) and (
        np.any(series.high < np.maximum(series.open, series.close))
        or np.any(series.low > np.minimum(series.open, series.close))
        or np.any(series.high < series.low)
        or np.any(series.low <= 0)
        or np.any(series.volume < 0)
    ):
        reasons.append("invalid_ohlcv_geometry")
    if len(series) > 1:
        deltas = np.diff(series.timestamp)
        interval = TIMEFRAME_MS.get(series.timeframe)
        if np.any(deltas <= 0):
            reasons.append("unordered_timestamps")
        if interval and np.any(deltas > interval * 3):
            reasons.append("missing_candles")
    interval = TIMEFRAME_MS.get(series.timeframe)
    if interval and len(series):
        age = series.as_of_ms - (series.latest_timestamp + interval)
        if age > interval * 2:
            reasons.append("stale_data")
        if series.latest_timestamp + interval > series.as_of_ms:
            reasons.append("forming_candle_present")
        if series.latest_timestamp > series.as_of_ms:
            reasons.append("future_timestamp")
    return DataQualityResult(not reasons, tuple(reasons))


def from_ccxt_rows(
    symbol: str,
    timeframe: str,
    rows: list[list[float]],
    as_of_ms: int,
) -> CandleSeries:
    interval = TIMEFRAME_MS[timeframe]
    normalized: list[list[float]] = []
    for row in rows[-250:]:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            values = [float(row[index]) for index in range(6)]
        except (TypeError, ValueError):
            continue
        normalized.append(values)
    # CCXT candle timestamps are opens. A candle is closed only after open + interval.
    closed = [row for row in normalized if int(row[0]) + interval <= as_of_ms]
    matrix = np.asarray(closed[-250:], dtype=np.float64)
    if matrix.size == 0:
        matrix = np.empty((0, 6), dtype=np.float64)
    return CandleSeries(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=matrix[:, 0].astype(np.int64, copy=False),
        open=matrix[:, 1],
        high=matrix[:, 2],
        low=matrix[:, 3],
        close=matrix[:, 4],
        volume=matrix[:, 5],
        as_of_ms=as_of_ms,
    )
