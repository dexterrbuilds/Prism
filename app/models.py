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


class SignalMode(StrEnum):
    INTRADAY = "INTRADAY"
    SCALP = "SCALP"


class EntryDecision(StrEnum):
    REJECT = "REJECT"
    WAIT = "WAIT"
    VALID = "VALID"
    HIGH_QUALITY = "HIGH_QUALITY"


class AIReviewVerdict(StrEnum):
    APPROVE = "APPROVE"
    WAIT = "WAIT"
    REJECT = "REJECT"
    UNAVAILABLE = "UNAVAILABLE"


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
    # V2 staged lifecycle. Existing values below remain deserializable.
    BIAS_DETECTED = "BIAS_DETECTED"
    SETUP_FORMING = "SETUP_FORMING"
    WAITING_FOR_ENTRY = "WAITING_FOR_ENTRY"
    ENTRY_READY = "ENTRY_READY"
    # Setup lifecycle used by newly published signals.
    CREATED = "CREATED"
    WAITING_ENTRY = "WAITING_ENTRY"
    ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
    MISSED = "MISSED"
    SL_HIT = "SL_HIT"

    # Legacy states remain readable so deployments can restore records created
    # before the setup-lifecycle migration without a destructive data rewrite.
    DETECTED = "DETECTED"
    WATCHING = "WATCHING"
    CONFIRMED = "CONFIRMED"
    ACTIVE = "ACTIVE"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    STOPPED = "STOPPED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    # A terminal, unscored outcome used when one candle contains both a stop
    # and an unachieved target and no finer candle path establishes ordering.
    AMBIGUOUS = "AMBIGUOUS"
    CANCELLED = "CANCELLED"


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
    ENTRY_QUALITY = "ENTRY_QUALITY"
    INSUFFICIENT_VOLATILITY = "INSUFFICIENT_VOLATILITY"


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
    mode: SignalMode = SignalMode.INTRADAY


@dataclass(frozen=True, slots=True)
class DirectionalBias:
    direction: Direction | None
    strength: float
    timeframe: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntryPlan:
    zone_low: float
    zone_high: float
    preferred_entry: float
    entry_type: str
    trigger: str
    source_levels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntryQuality:
    total: int
    decision: EntryDecision
    categories: Mapping[str, int]
    evidence: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    hard_reasons: tuple[str, ...] = ()
    retest_completed: bool = False
    lower_timeframe_confirmed: bool = False
    distance_from_entry_atr: float = 0.0


@dataclass(frozen=True, slots=True)
class AIReview:
    verdict: AIReviewVerdict
    reasoning: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    reviewed_at: datetime | None = None


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
    tp4: float | None = None


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
    current_price: float | None = None
    state_changed_at: datetime | None = None
    activated_at: datetime | None = None
    tp1_hit_at: datetime | None = None
    trading_timeframe: str = "15m"
    analysis_timeframe: str = "1h"
    expires_at: datetime | None = None
    validity_minutes: int | None = None
    valid_conditions: tuple[str, ...] = ()
    max_missed_distance: float | None = None
    entry_trigger_price: float | None = None
    missed_at: datetime | None = None
    invalidated_at: datetime | None = None
    expired_at: datetime | None = None
    tp2_hit_at: datetime | None = None
    stopped_at: datetime | None = None
    lifecycle_reason: str | None = None
    mode: SignalMode = SignalMode.INTRADAY
    entry_quality: EntryQuality | None = None
    ai_review: AIReview | None = None
    atr_at_entry: float | None = None
    mae: float = 0.0
    mfe: float = 0.0
    stopped_then_target_reached: bool = False
    follow_up_until: datetime | None = None
    directional_bias: DirectionalBias | None = None
    # Lifecycle identity is always ``id``.  The fingerprint groups materially
    # identical market opportunities without merging issued trade instances.
    setup_fingerprint: str = ""
    signal_type: str = "INITIAL"
    parent_signal_id: str | None = None
    setup_origin_at: datetime | None = None
    major_structure_level: float | None = None
    last_evaluated_at: datetime | None = None
    terminal_state: str | None = None
    terminal_at: datetime | None = None
    result: str | None = None

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(UTC)
