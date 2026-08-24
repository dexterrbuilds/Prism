from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class MarketRegime(StrEnum):
    STRONG_BULLISH_TREND = "STRONG_BULLISH_TREND"
    BULLISH_TREND = "BULLISH_TREND"
    STRONG_BEARISH_TREND = "STRONG_BEARISH_TREND"
    BEARISH_TREND = "BEARISH_TREND"
    RANGE = "RANGE"
    COMPRESSION = "COMPRESSION"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNCLEAR = "UNCLEAR"


class StructureBias(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGE = "RANGE"
    UNCLEAR = "UNCLEAR"


class SwingKind(StrEnum):
    HIGH = "HIGH"
    LOW = "LOW"


class SwingLabel(StrEnum):
    HH = "HH"
    HL = "HL"
    LH = "LH"
    LL = "LL"
    HIGH = "HIGH"
    LOW = "LOW"


class ZoneKind(StrEnum):
    SUPPORT = "SUPPORT"
    RESISTANCE = "RESISTANCE"
    MIXED = "MIXED"


class VolatilityClass(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    EXPANDING = "EXPANDING"
    HIGH = "HIGH"


class SignalState(StrEnum):
    DETECTED = "DETECTED"
    WATCHING = "WATCHING"
    CONFIRMED = "CONFIRMED"
    ACTIVE = "ACTIVE"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    STOPPED = "STOPPED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class SignalGrade(StrEnum):
    IGNORE = "IGNORE"
    WATCH = "WATCH"
    VALID = "VALID"
    EXCEPTIONAL = "EXCEPTIONAL"


class RejectionReason(StrEnum):
    NO_SETUP = "NO_SETUP"
    LOW_CONFLUENCE = "LOW_CONFLUENCE"
    POOR_RR = "POOR_RR"
    ENTRY_TOO_LATE = "ENTRY_TOO_LATE"
    HTF_CONFLICT = "HTF_CONFLICT"
    INSUFFICIENT_VOLUME = "INSUFFICIENT_VOLUME"
    UNCLEAR_STRUCTURE = "UNCLEAR_STRUCTURE"
    UNCLEAR_REGIME = "UNCLEAR_REGIME"
    INSUFFICIENT_ROOM_TO_TARGET = "INSUFFICIENT_ROOM_TO_TARGET"
    DATA_QUALITY = "DATA_QUALITY"
    EXCESSIVE_VOLATILITY = "EXCESSIVE_VOLATILITY"
    INVALIDATED_PATTERN = "INVALIDATED_PATTERN"


@dataclass(frozen=True, slots=True)
class CandleSeries:
    symbol: str
    timeframe: str
    timestamp: IntArray
    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray
    volume: FloatArray
    as_of_ms: int

    def __len__(self) -> int:
        return int(self.close.size)

    @property
    def latest_close(self) -> float:
        return float(self.close[-1])

    @property
    def latest_timestamp(self) -> int:
        return int(self.timestamp[-1])


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    symbol: str
    series: Mapping[str, CandleSeries]
    as_of_ms: int


@dataclass(frozen=True, slots=True)
class SwingPoint:
    index: int
    timestamp: int
    price: float
    kind: SwingKind
    label: SwingLabel
    confirmed_at_index: int


@dataclass(frozen=True, slots=True)
class StructureEvent:
    name: str
    direction: Direction
    index: int
    level: float
    confirmed: bool = True


@dataclass(frozen=True, slots=True)
class StructureState:
    bias: StructureBias
    swings: tuple[SwingPoint, ...]
    events: tuple[StructureEvent, ...]
    significant_high: float | None
    significant_low: float | None
    range_high: float | None
    range_low: float | None
    previous_day_high: float | None = None
    previous_day_low: float | None = None
    previous_week_high: float | None = None
    previous_week_low: float | None = None


@dataclass(frozen=True, slots=True)
class SupportResistanceZone:
    low: float
    high: float
    kind: ZoneKind
    score: float
    reactions: int
    sources: tuple[str, ...]
    last_index: int

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass(frozen=True, slots=True)
class PatternDetection:
    name: str
    direction: Direction | None
    quality: float
    start_index: int
    end_index: int
    breakout_level: float | None = None
    evidence: tuple[str, ...] = ()


@dataclass(slots=True)
class ConfluenceEvidence:
    trend: list[str] = field(default_factory=list)
    structure: list[str] = field(default_factory=list)
    location: list[str] = field(default_factory=list)
    momentum: list[str] = field(default_factory=list)
    volume: list[str] = field(default_factory=list)
    pattern: list[str] = field(default_factory=list)
    candlestick: list[str] = field(default_factory=list)
    volatility: list[str] = field(default_factory=list)
    higher_timeframe: list[str] = field(default_factory=list)

    def as_mapping(self) -> dict[str, list[str]]:
        return {name: list(getattr(self, name)) for name in self.__dataclass_fields__}

    def flattened(self) -> tuple[str, ...]:
        return tuple(item for values in self.as_mapping().values() for item in values)


@dataclass(frozen=True, slots=True)
class SetupCandidate:
    symbol: str
    strategy: str
    direction: Direction
    timeframe: str
    detected_at_ms: int
    ideal_entry_low: float
    ideal_entry_high: float
    trigger: str
    invalidation_level: float
    quality: float
    evidence: ConfluenceEvidence
    confirmed: bool
    metadata: Mapping[str, float | str | bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TradePlan:
    entry_zone_low: float
    entry_zone_high: float
    preferred_entry: float
    entry_type: str
    trigger: str
    stop_loss: float
    risk_per_unit: float
    stop_distance_atr: float
    invalidation_reason: str
    tp1: float
    tp2: float
    tp3: float | None
    reward_risk: float
    estimated_hold_hours_low: float | None = None
    estimated_hold_hours_high: float | None = None
    invalidation_level: float | None = None


@dataclass(frozen=True, slots=True)
class Signal:
    id: str
    symbol: str
    strategy: str
    direction: Direction
    regime: MarketRegime
    score: int
    grade: SignalGrade
    state: SignalState
    trade: TradePlan
    evidence: tuple[str, ...]
    created_at: datetime
    supporting_strategies: tuple[str, ...] = ()

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(UTC)
