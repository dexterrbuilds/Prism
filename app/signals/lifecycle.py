from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from app.models import Direction, EntryQuality, PublicationState, Signal, SignalMode, SignalState

WAITING_STATES = frozenset(
    {
        SignalState.BIAS_DETECTED,
        SignalState.SETUP_FORMING,
        SignalState.WAITING_FOR_ENTRY,
        SignalState.CREATED,
        SignalState.WAITING_ENTRY,
        SignalState.CONFIRMED,
    }
)
READY_STATES = frozenset({SignalState.ENTRY_READY})
ACTIVE_STATES = frozenset({SignalState.ENTRY_TRIGGERED, SignalState.ACTIVE})
STOP_STATES = frozenset({SignalState.SL_HIT, SignalState.STOPPED})
TERMINAL_PRE_ENTRY_STATES = frozenset(
    {SignalState.MISSED, SignalState.INVALIDATED, SignalState.EXPIRED, SignalState.CANCELLED}
)
TERMINAL_STATES = TERMINAL_PRE_ENTRY_STATES | STOP_STATES | frozenset(
    {SignalState.TP2_HIT, SignalState.AMBIGUOUS}
)
OPEN_STATES = WAITING_STATES | READY_STATES | ACTIVE_STATES | frozenset({SignalState.TP1_HIT})
# Stopped trades remain briefly observable only for stopped-then-target analytics.
TRACKED_STATES = OPEN_STATES | STOP_STATES

ALLOWED_TRANSITIONS: dict[SignalState, frozenset[SignalState]] = {
    SignalState.BIAS_DETECTED: frozenset(
        {SignalState.SETUP_FORMING, SignalState.WAITING_FOR_ENTRY, SignalState.INVALIDATED, SignalState.EXPIRED}
    ),
    SignalState.SETUP_FORMING: frozenset(
        {SignalState.WAITING_FOR_ENTRY, SignalState.ENTRY_READY, SignalState.MISSED, SignalState.INVALIDATED, SignalState.EXPIRED}
    ),
    SignalState.WAITING_FOR_ENTRY: frozenset(
        {SignalState.ENTRY_READY, SignalState.MISSED, SignalState.INVALIDATED, SignalState.EXPIRED, SignalState.CANCELLED}
    ),
    SignalState.ENTRY_READY: frozenset(
        {SignalState.ENTRY_TRIGGERED, SignalState.WAITING_FOR_ENTRY, SignalState.MISSED, SignalState.INVALIDATED, SignalState.EXPIRED, SignalState.CANCELLED}
    ),
    SignalState.CREATED: frozenset(
        {
            SignalState.WAITING_ENTRY,
            SignalState.WAITING_FOR_ENTRY,
            SignalState.SETUP_FORMING,
            SignalState.ENTRY_TRIGGERED,
            SignalState.MISSED,
            SignalState.INVALIDATED,
            SignalState.EXPIRED,
            SignalState.CANCELLED,
        }
    ),
    SignalState.WAITING_ENTRY: frozenset(
        {SignalState.ENTRY_TRIGGERED, SignalState.MISSED, SignalState.INVALIDATED, SignalState.EXPIRED, SignalState.CANCELLED}
    ),
    SignalState.ENTRY_TRIGGERED: frozenset(
        {SignalState.TP1_HIT, SignalState.TP2_HIT, SignalState.SL_HIT, SignalState.AMBIGUOUS}
    ),
    SignalState.MISSED: frozenset(),
    SignalState.SL_HIT: frozenset(),
    SignalState.DETECTED: frozenset(
        {SignalState.WATCHING, SignalState.CONFIRMED, SignalState.INVALIDATED, SignalState.EXPIRED}
    ),
    SignalState.WATCHING: frozenset(
        {SignalState.CONFIRMED, SignalState.INVALIDATED, SignalState.EXPIRED}
    ),
    SignalState.CONFIRMED: frozenset(
        {
            SignalState.ACTIVE,
            SignalState.ENTRY_TRIGGERED,
            SignalState.MISSED,
            SignalState.INVALIDATED,
            SignalState.EXPIRED,
            SignalState.CANCELLED,
        }
    ),
    SignalState.ACTIVE: frozenset(
        {SignalState.TP1_HIT, SignalState.TP2_HIT, SignalState.STOPPED, SignalState.SL_HIT, SignalState.AMBIGUOUS}
    ),
    SignalState.TP1_HIT: frozenset(
        {SignalState.TP2_HIT, SignalState.STOPPED, SignalState.SL_HIT, SignalState.AMBIGUOUS}
    ),
    SignalState.TP2_HIT: frozenset(),
    SignalState.STOPPED: frozenset(),
    SignalState.INVALIDATED: frozenset(),
    SignalState.EXPIRED: frozenset(),
    SignalState.AMBIGUOUS: frozenset(),
    SignalState.CANCELLED: frozenset(),
}


def transition(
    signal: Signal,
    target: SignalState,
    *,
    current_price: float | None = None,
    changed_at: datetime | None = None,
    trigger_price: float | None = None,
    reason: str | None = None,
) -> Signal:
    if target not in ALLOWED_TRANSITIONS[signal.state]:
        raise ValueError(f"invalid signal transition {signal.state.value} -> {target.value}")
    event_at = changed_at or datetime.now(UTC)
    activated = target in ACTIVE_STATES
    stopped = target in STOP_STATES
    follow_up_until = signal.follow_up_until
    if stopped and signal.tp1_hit_at is None and follow_up_until is None:
        hours = signal.trade.estimated_hold_hours_high or 24.0
        follow_up_until = event_at + timedelta(hours=max(1.0, min(120.0, hours)))

    result = signal.result
    if target in {SignalState.TP1_HIT, SignalState.TP2_HIT}:
        result = "WIN"
    elif stopped and signal.tp1_hit_at is None:
        result = "LOSS"
    elif target is SignalState.AMBIGUOUS and result is None:
        result = "AMBIGUOUS"
    elif target in TERMINAL_PRE_ENTRY_STATES:
        result = "NO_TRADE"
    terminal = target in TERMINAL_STATES

    publication_state = signal.publication_state
    if target is SignalState.ENTRY_READY and publication_state is PublicationState.INTERNAL_ONLY:
        publication_state = PublicationState.PUBLISH_PENDING

    return replace(
        signal,
        state=target,
        current_price=signal.current_price if current_price is None else current_price,
        state_changed_at=event_at,
        last_evaluated_at=event_at,
        activated_at=event_at if activated and signal.activated_at is None else signal.activated_at,
        entry_trigger_price=(
            trigger_price if activated and signal.entry_trigger_price is None else signal.entry_trigger_price
        ),
        missed_at=event_at if target is SignalState.MISSED and signal.missed_at is None else signal.missed_at,
        invalidated_at=(
            event_at if target is SignalState.INVALIDATED and signal.invalidated_at is None else signal.invalidated_at
        ),
        expired_at=event_at if target is SignalState.EXPIRED and signal.expired_at is None else signal.expired_at,
        tp1_hit_at=(
            event_at
            if target in {SignalState.TP1_HIT, SignalState.TP2_HIT} and signal.tp1_hit_at is None
            else signal.tp1_hit_at
        ),
        tp2_hit_at=event_at if target is SignalState.TP2_HIT and signal.tp2_hit_at is None else signal.tp2_hit_at,
        stopped_at=event_at if stopped and signal.stopped_at is None else signal.stopped_at,
        lifecycle_reason=reason if reason is not None else signal.lifecycle_reason,
        follow_up_until=follow_up_until,
        terminal_state=target.value if terminal else signal.terminal_state,
        terminal_at=event_at if terminal and signal.terminal_at is None else signal.terminal_at,
        result=result,
        publication_state=publication_state,
    )


def signal_key(symbol: str, direction: Direction) -> str:
    """Secondary lookup label only; never a lifecycle identity."""
    return f"{symbol}|{direction.value}"


def strategy_family(strategy: str) -> str:
    value = strategy.upper()
    if "LIQUIDITY" in value or "SWEEP" in value or "FAILED" in value:
        return "LIQUIDITY_REVERSAL"
    if "BREAKOUT" in value or "BREAKDOWN" in value or "TRENDLINE" in value:
        return "BREAKOUT"
    if "FLAG" in value or "TRIANGLE" in value or "WEDGE" in value:
        return "CHART_PATTERN"
    if "PULLBACK" in value or "BOS_CONTINUATION" in value:
        return "TREND_CONTINUATION"
    if "REVERSAL" in value or "HEAD_AND_SHOULDERS" in value:
        return "REVERSAL"
    if "RANGE" in value or "MEAN_REVERSION" in value or "REJECTION" in value:
        return "RANGE"
    if "MOMENTUM" in value or "VOLATILITY" in value:
        return "MOMENTUM"
    return value


def _regime_family(value: str) -> str:
    if value in {"STRONG_BULLISH_TREND", "BULLISH_TREND"}:
        return "BULLISH_TREND"
    if value in {"STRONG_BEARISH_TREND", "BEARISH_TREND"}:
        return "BEARISH_TREND"
    if value in {"RANGE", "COMPRESSION"}:
        return "NON_TRENDING"
    return value


def _quantized(value: float, atr: float, step_atr: float = 0.25) -> int:
    step = max(abs(atr) * step_atr, abs(value) * 1e-8, 1e-12)
    return round(value / step)


def build_setup_fingerprint(
    *,
    symbol: str,
    direction: Direction,
    mode: SignalMode,
    strategy: str,
    regime: str,
    entry_low: float,
    entry_high: float,
    invalidation: float,
    major_structure_level: float,
    atr: float,
    setup_origin_ms: int | None,
) -> str:
    """Stable identity for one market opportunity, independent of scan time."""
    values = (
        symbol,
        direction.value,
        mode.value,
        strategy_family(strategy),
        regime,
        _quantized((entry_low + entry_high) / 2, atr),
        _quantized(invalidation, atr),
        _quantized(major_structure_level, atr),
        int(setup_origin_ms or 0),
    )
    return sha256("|".join(map(str, values)).encode()).hexdigest()[:24]


def create_signal_id(symbol: str, direction: Direction, created_at: datetime) -> str:
    base = symbol.split("/", maxsplit=1)[0].replace(":", "")[:10]
    stamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"SIG-{base}-{direction.value[0]}-{stamp}-{uuid4().hex[:10]}"


def _dedup_phase(state: SignalState) -> str:
    if state in WAITING_STATES:
        return "WAITING_ENTRY"
    if state in ACTIVE_STATES:
        return "ENTRY_TRIGGERED"
    if state in READY_STATES:
        return "ENTRY_READY"
    if state in STOP_STATES:
        return "SL_HIT"
    return state.value


def signal_fingerprint(signal: Signal) -> str:
    """Alert-event fingerprint retained for backward-compatible diagnostics."""
    raw = f"{signal.id}|{signal.setup_fingerprint}|{_dedup_phase(signal.state)}"
    return sha256(raw.encode()).hexdigest()[:20]


def _entry_touch_price(signal: Signal, close: float) -> float:
    return min(max(close, signal.trade.entry_zone_low), signal.trade.entry_zone_high)


def _invalidation_level(signal: Signal) -> float:
    return signal.trade.invalidation_level or signal.trade.stop_loss


def _atr(signal: Signal) -> float:
    if signal.atr_at_entry is not None and signal.atr_at_entry > 0:
        return signal.atr_at_entry
    if signal.trade.stop_distance_atr > 0:
        return signal.trade.risk_per_unit / signal.trade.stop_distance_atr
    return max(signal.trade.risk_per_unit, 1e-12)


def _zones_overlap(left: Signal, right: Signal) -> float:
    overlap = max(
        0.0,
        min(left.trade.entry_zone_high, right.trade.entry_zone_high)
        - max(left.trade.entry_zone_low, right.trade.entry_zone_low),
    )
    narrower = min(
        left.trade.entry_zone_high - left.trade.entry_zone_low,
        right.trade.entry_zone_high - right.trade.entry_zone_low,
    )
    return overlap / narrower if narrower > 0 else float(overlap == 0)


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    signal: Signal
    reason: str


class SignalStore:
    """Bounded per-instance state machines keyed exclusively by immutable signal ID."""

    def __init__(self, max_size: int = 128) -> None:
        self._signals: OrderedDict[str, Signal] = OrderedDict()
        self._max_size = max_size

    def restore(self, signal: Signal) -> None:
        """Restore one persisted instance without merging same-symbol signals."""
        self._signals[signal.id] = signal
        self._signals.move_to_end(signal.id)
        self._trim()

    def find_duplicate(
        self,
        signal: Signal,
        *,
        window_minutes: int = 360,
        entry_atr: float = 0.20,
        stop_atr: float = 0.25,
        target_atr: float = 0.25,
    ) -> DuplicateMatch | None:
        for existing in reversed(tuple(self._signals.values())):
            # A candidate already registered for lifecycle monitoring must not
            # suppress its own first publication.
            if existing.id == signal.id:
                continue
            if existing.state not in OPEN_STATES:
                continue
            if (
                existing.symbol != signal.symbol
                or existing.direction is not signal.direction
                or existing.mode is not signal.mode
            ):
                continue
            # A confirmed new structural origin is a hard new-opportunity
            # boundary.  Elapsed time must never override this test.
            if (
                signal.setup_origin_at is not None
                and existing.setup_origin_at is not None
                and signal.setup_origin_at != existing.setup_origin_at
            ):
                continue
            if signal.setup_fingerprint and existing.setup_fingerprint == signal.setup_fingerprint:
                return DuplicateMatch(existing, "same setup fingerprint remains non-terminal")
            if _regime_family(existing.regime.value) != _regime_family(signal.regime.value):
                continue
            age = abs((signal.created_at - existing.created_at).total_seconds()) / 60
            atr = max(_atr(signal), _atr(existing), 1e-12)
            entry_close = abs(signal.trade.preferred_entry - existing.trade.preferred_entry) <= entry_atr * atr
            entry_close = entry_close or _zones_overlap(signal, existing) >= 0.50
            stop_close = abs(signal.trade.stop_loss - existing.trade.stop_loss) <= stop_atr * atr
            targets_close = (
                abs(signal.trade.tp1 - existing.trade.tp1) <= target_atr * atr
                and abs(signal.trade.tp2 - existing.trade.tp2) <= target_atr * atr
            )
            structure_close = (
                signal.major_structure_level is None
                or existing.major_structure_level is None
                or abs(signal.major_structure_level - existing.major_structure_level) <= 0.25 * atr
            )
            same_family = strategy_family(existing.strategy) == strategy_family(signal.strategy)
            if entry_close and stop_close and targets_close and structure_close:
                timing = (
                    f"within the {window_minutes}-minute supporting window"
                    if age <= window_minutes
                    else "despite being outside the supporting time window"
                )
                label = "same strategy family" if same_family else "different labels on the same structural opportunity"
                return DuplicateMatch(
                    existing,
                    f"ATR-normalized geometry and structural origin match ({label}; {timing})",
                )
        return None

    def find_reentry_parent(self, signal: Signal) -> Signal | None:
        """Return an active parent only for a newly confirmed entry opportunity.

        A price tweak is not a re-entry.  The candidate needs a deterministic
        closed-candle entry trigger and a materially new structural origin (or,
        for legacy data without origins, a materially changed structure level).
        """
        quality = signal.entry_quality
        if (
            signal.state is not SignalState.ENTRY_READY
            or quality is None
            or not quality.retest_completed
            or not quality.lower_timeframe_confirmed
        ):
            return None
        for existing in reversed(tuple(self._signals.values())):
            if (
                existing.symbol != signal.symbol
                or existing.direction is not signal.direction
                or existing.mode is not signal.mode
                or existing.state not in ACTIVE_STATES | frozenset({SignalState.TP1_HIT})
            ):
                continue
            origin_changed = (
                signal.setup_origin_at is not None
                and existing.setup_origin_at is not None
                and signal.setup_origin_at != existing.setup_origin_at
            )
            atr = max(_atr(signal), _atr(existing), 1e-12)
            structure_changed = (
                signal.major_structure_level is not None
                and existing.major_structure_level is not None
                and abs(signal.major_structure_level - existing.major_structure_level) > 0.25 * atr
            )
            if origin_changed or structure_changed:
                return existing
        return None

    def should_publish(
        self,
        signal: Signal,
        *,
        window_minutes: int = 360,
        entry_atr: float = 0.20,
        stop_atr: float = 0.25,
        target_atr: float = 0.25,
    ) -> bool:
        if self.find_duplicate(
            signal,
            window_minutes=window_minutes,
            entry_atr=entry_atr,
            stop_atr=stop_atr,
            target_atr=target_atr,
        ):
            return False
        self.restore(signal)
        return True

    def _trim(self) -> None:
        while len(self._signals) > self._max_size:
            removable = next(
                (key for key, value in self._signals.items() if value.state not in TRACKED_STATES),
                None,
            )
            if removable is None:
                break
            self._signals.pop(removable, None)

    def get(self, signal_id: str) -> Signal | None:
        return self._signals.get(signal_id)

    def discard(self, signal_id: str) -> None:
        self._signals.pop(signal_id, None)

    def open_symbols(self) -> tuple[str, ...]:
        now = datetime.now(UTC)
        return tuple(
            dict.fromkeys(
                signal.symbol
                for signal in self._signals.values()
                if signal.state in OPEN_STATES
                or (
                    signal.state in STOP_STATES
                    and not signal.stopped_then_target_reached
                    and signal.follow_up_until is not None
                    and now < signal.follow_up_until
                )
            )
        )

    def open_signals(self) -> tuple[Signal, ...]:
        return tuple(signal for signal in self._signals.values() if signal.state in OPEN_STATES)

    def signals_for_symbol(self, symbol: str) -> tuple[Signal, ...]:
        return tuple(signal for signal in self._signals.values() if signal.symbol == symbol)

    def concurrent_open_count(self, symbol: str) -> int:
        return sum(signal.symbol == symbol and signal.state in OPEN_STATES for signal in self._signals.values())

    def mark_entry_ready(
        self,
        signal_id: str,
        quality: EntryQuality,
        *,
        observed_at: datetime,
        current_price: float,
        minimum_score: int = 75,
    ) -> Signal | None:
        signal = self._signals.get(signal_id)
        if signal is None or signal.state not in WAITING_STATES:
            return None
        if (
            quality.total < minimum_score
            or quality.hard_reasons
            or not quality.retest_completed
            or not quality.lower_timeframe_confirmed
        ):
            self._signals[signal_id] = replace(
                signal,
                current_price=current_price,
                entry_quality=quality,
                last_evaluated_at=observed_at,
            )
            return None
        updated = transition(
            replace(signal, entry_quality=quality),
            SignalState.ENTRY_READY,
            current_price=current_price,
            changed_at=observed_at,
            reason="Closed lower-timeframe retest and structure confirmation passed the entry-quality gate.",
        )
        self._signals[signal_id] = updated
        return updated

    def expire_due(self, observed_at: datetime | None = None) -> list[Signal]:
        now = observed_at or datetime.now(UTC)
        events: list[Signal] = []
        for signal_id, signal in list(self._signals.items()):
            if (
                signal.state not in WAITING_STATES | READY_STATES
                or signal.expires_at is None
                or now < signal.expires_at
                or (signal.state_changed_at is not None and now <= signal.state_changed_at)
            ):
                continue
            updated = transition(
                signal,
                SignalState.EXPIRED,
                current_price=signal.current_price,
                changed_at=now,
                reason="Setup validity window elapsed before the entry zone was triggered.",
            )
            self._signals[signal_id] = updated
            events.append(updated)
        return events

    def track_price(
        self,
        symbol: str,
        price: float,
        *,
        observed_at: datetime | None = None,
    ) -> list[Signal]:
        return self._track_observation(
            symbol,
            price,
            price,
            price,
            observed_at or datetime.now(UTC),
            connect_previous=True,
        )

    def track_candles(
        self,
        symbol: str,
        timestamps_ms: Iterable[int],
        highs: Iterable[float],
        lows: Iterable[float],
        closes: Iterable[float],
        timeframe_ms: int = 900_000,
        trading_timeframe: str | None = None,
    ) -> list[Signal]:
        events: list[Signal] = []
        for timestamp, high, low, close in zip(timestamps_ms, highs, lows, closes, strict=True):
            event_at = datetime.fromtimestamp((int(timestamp) + timeframe_ms) / 1000, UTC)
            events.extend(
                self._track_observation(
                    symbol,
                    float(high),
                    float(low),
                    float(close),
                    event_at,
                    trading_timeframe=trading_timeframe,
                )
            )
        return events

    def _track_observation(
        self,
        symbol: str,
        high: float,
        low: float,
        close: float,
        event_at: datetime,
        *,
        connect_previous: bool = False,
        trading_timeframe: str | None = None,
    ) -> list[Signal]:
        events: list[Signal] = []
        for signal_id, stored in list(self._signals.items()):
            signal = stored
            if (
                signal.symbol != symbol
                or signal.state not in TRACKED_STATES
                or (trading_timeframe is not None and signal.trading_timeframe != trading_timeframe)
            ):
                continue
            cursor = signal.last_evaluated_at or signal.state_changed_at
            if cursor is not None and event_at <= cursor:
                continue
            observed_high = high
            observed_low = low
            if connect_previous and signal.current_price is not None:
                observed_high = max(observed_high, signal.current_price)
                observed_low = min(observed_low, signal.current_price)
            long = signal.direction is Direction.LONG
            if signal.activated_at is not None:
                entry = signal.entry_trigger_price or signal.trade.preferred_entry
                adverse = max(0.0, entry - observed_low) if long else max(0.0, observed_high - entry)
                favorable = max(0.0, observed_high - entry) if long else max(0.0, entry - observed_low)
                signal = replace(
                    signal,
                    mae=max(signal.mae, adverse),
                    mfe=max(signal.mfe, favorable),
                    current_price=close,
                    last_evaluated_at=event_at,
                )
                self._signals[signal_id] = signal
            if signal.state in STOP_STATES:
                target_reached = (long and observed_high >= signal.trade.tp2) or (
                    not long and observed_low <= signal.trade.tp2
                )
                self._signals[signal_id] = replace(
                    signal,
                    stopped_then_target_reached=signal.stopped_then_target_reached or target_reached,
                    current_price=close,
                    last_evaluated_at=event_at,
                )
                continue

            invalidation_level = _invalidation_level(signal)
            invalidation_breached = (long and observed_low <= invalidation_level) or (
                not long and observed_high >= invalidation_level
            )
            stop_breached = (long and observed_low <= signal.trade.stop_loss) or (
                not long and observed_high >= signal.trade.stop_loss
            )
            tp1_breached = (long and observed_high >= signal.trade.tp1) or (
                not long and observed_low <= signal.trade.tp1
            )
            tp2_breached = (long and observed_high >= signal.trade.tp2) or (
                not long and observed_low <= signal.trade.tp2
            )

            updated: Signal | None = None
            if signal.state in WAITING_STATES | READY_STATES:
                if signal.expires_at is not None and event_at >= signal.expires_at:
                    updated = transition(
                        signal,
                        SignalState.EXPIRED,
                        current_price=close,
                        changed_at=event_at,
                        reason="Setup validity window elapsed before the entry zone was triggered.",
                    )
                else:
                    entry_touched = observed_low <= signal.trade.entry_zone_high and observed_high >= signal.trade.entry_zone_low
                    legacy_activation = signal.state in {SignalState.CREATED, SignalState.WAITING_ENTRY, SignalState.CONFIRMED}
                    if entry_touched and (signal.state in READY_STATES or legacy_activation):
                        active_state = SignalState.ACTIVE if signal.state is SignalState.CONFIRMED else SignalState.ENTRY_TRIGGERED
                        activated = transition(
                            signal,
                            active_state,
                            current_price=close,
                            changed_at=event_at,
                            trigger_price=_entry_touch_price(signal, close),
                            reason="Price entered the configured entry zone while the setup was valid.",
                        )
                        self._signals[signal_id] = activated
                        events.append(activated)
                        if stop_breached and (tp1_breached or tp2_breached):
                            updated = transition(
                                activated,
                                SignalState.AMBIGUOUS,
                                current_price=close,
                                changed_at=event_at,
                                reason="Entry, stop, and target occurred inside one candle; event ordering is unavailable.",
                            )
                        elif stop_breached or invalidation_breached:
                            stop_state = SignalState.STOPPED if active_state is SignalState.ACTIVE else SignalState.SL_HIT
                            updated = transition(
                                activated,
                                stop_state,
                                current_price=signal.trade.stop_loss,
                                changed_at=event_at,
                                reason="Stop/invalidation was reached in the entry candle; conservative loss ordering applied.",
                            )
                        else:
                            continue
                    elif invalidation_breached:
                        relation = "below" if long else "above"
                        updated = transition(
                            signal,
                            SignalState.INVALIDATED,
                            current_price=close,
                            changed_at=event_at,
                            reason=f"Price broke {relation} the setup invalidation level {invalidation_level:.8g} before entry.",
                        )
                    elif signal.max_missed_distance is not None and (
                        (long and close > signal.trade.entry_zone_high + signal.max_missed_distance)
                        or (not long and close < signal.trade.entry_zone_low - signal.max_missed_distance)
                    ):
                        updated = transition(
                            signal,
                            SignalState.MISSED,
                            current_price=close,
                            changed_at=event_at,
                            reason="Price moved beyond the actionable entry area without triggering the setup.",
                        )
                    else:
                        self._signals[signal_id] = replace(signal, current_price=close, last_evaluated_at=event_at)
                        continue
            elif signal.state in ACTIVE_STATES:
                if stop_breached and (tp1_breached or tp2_breached):
                    updated = transition(
                        signal,
                        SignalState.AMBIGUOUS,
                        current_price=close,
                        changed_at=event_at,
                        reason="Stop and target were both touched inside one candle; lower-timeframe ordering was unavailable.",
                    )
                elif stop_breached:
                    updated = transition(
                        signal,
                        SignalState.STOPPED if signal.state is SignalState.ACTIVE else SignalState.SL_HIT,
                        current_price=signal.trade.stop_loss,
                        changed_at=event_at,
                        reason="The active trade reached its structural stop level.",
                    )
                elif tp2_breached:
                    updated = transition(
                        signal,
                        SignalState.TP2_HIT,
                        current_price=signal.trade.tp2,
                        changed_at=event_at,
                        reason="The active trade reached TP2.",
                    )
                elif tp1_breached:
                    updated = transition(
                        signal,
                        SignalState.TP1_HIT,
                        current_price=signal.trade.tp1,
                        changed_at=event_at,
                        reason="The active trade reached TP1; a win is now recorded.",
                    )
                else:
                    self._signals[signal_id] = replace(signal, current_price=close, last_evaluated_at=event_at)
                    continue
            elif signal.state is SignalState.TP1_HIT:
                if stop_breached and tp2_breached:
                    updated = transition(
                        signal,
                        SignalState.AMBIGUOUS,
                        current_price=close,
                        changed_at=event_at,
                        reason="Runner stop and TP2 were both touched inside one candle; TP1 win remains recorded.",
                    )
                elif stop_breached:
                    updated = transition(
                        signal,
                        SignalState.SL_HIT if signal.entry_trigger_price is not None else SignalState.STOPPED,
                        current_price=signal.trade.stop_loss,
                        changed_at=event_at,
                        reason="The TP1 runner later reached its structural stop; the TP1 win remains recorded.",
                    )
                elif tp2_breached:
                    updated = transition(
                        signal,
                        SignalState.TP2_HIT,
                        current_price=signal.trade.tp2,
                        changed_at=event_at,
                        reason="The active trade reached TP2.",
                    )
                else:
                    self._signals[signal_id] = replace(signal, current_price=close, last_evaluated_at=event_at)
                    continue

            assert updated is not None
            self._signals[signal_id] = updated
            events.append(updated)
        return events

    def __len__(self) -> int:
        return len(self._signals)
