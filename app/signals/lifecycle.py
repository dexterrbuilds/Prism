from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256

from app.models import Direction, Signal, SignalState

WAITING_STATES = frozenset(
    {
        SignalState.CREATED,
        SignalState.WAITING_ENTRY,
        # Backward-compatible waiting state used by already persisted signals.
        SignalState.CONFIRMED,
    }
)
ACTIVE_STATES = frozenset(
    {
        SignalState.ENTRY_TRIGGERED,
        # Backward-compatible active state used by already persisted signals.
        SignalState.ACTIVE,
    }
)
STOP_STATES = frozenset({SignalState.SL_HIT, SignalState.STOPPED})
TERMINAL_PRE_ENTRY_STATES = frozenset({SignalState.MISSED, SignalState.INVALIDATED, SignalState.EXPIRED})
OPEN_STATES = WAITING_STATES | ACTIVE_STATES | frozenset({SignalState.TP1_HIT})

ALLOWED_TRANSITIONS: dict[SignalState, frozenset[SignalState]] = {
    SignalState.CREATED: frozenset(
        {
            SignalState.WAITING_ENTRY,
            SignalState.ENTRY_TRIGGERED,
            SignalState.MISSED,
            SignalState.INVALIDATED,
            SignalState.EXPIRED,
        }
    ),
    SignalState.WAITING_ENTRY: frozenset(
        {SignalState.ENTRY_TRIGGERED, SignalState.MISSED, SignalState.INVALIDATED, SignalState.EXPIRED}
    ),
    SignalState.ENTRY_TRIGGERED: frozenset({SignalState.TP1_HIT, SignalState.TP2_HIT, SignalState.SL_HIT}),
    SignalState.MISSED: frozenset(),
    SignalState.SL_HIT: frozenset(),
    # Legacy transition graph. New signals do not enter these states, but old
    # records must remain monitorable and preserve their historical outcomes.
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
        }
    ),
    SignalState.ACTIVE: frozenset(
        {SignalState.TP1_HIT, SignalState.TP2_HIT, SignalState.STOPPED, SignalState.SL_HIT}
    ),
    SignalState.TP1_HIT: frozenset(
        {SignalState.TP2_HIT, SignalState.STOPPED, SignalState.SL_HIT}
    ),
    SignalState.TP2_HIT: frozenset(),
    SignalState.STOPPED: frozenset(),
    SignalState.INVALIDATED: frozenset(),
    SignalState.EXPIRED: frozenset(),
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
    return replace(
        signal,
        state=target,
        current_price=signal.current_price if current_price is None else current_price,
        state_changed_at=event_at,
        activated_at=event_at if activated and signal.activated_at is None else signal.activated_at,
        entry_trigger_price=(
            trigger_price
            if activated and signal.entry_trigger_price is None
            else signal.entry_trigger_price
        ),
        missed_at=event_at if target is SignalState.MISSED and signal.missed_at is None else signal.missed_at,
        invalidated_at=(
            event_at
            if target is SignalState.INVALIDATED and signal.invalidated_at is None
            else signal.invalidated_at
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
    )


def signal_key(symbol: str, direction: Direction) -> str:
    """One active directional thesis per symbol, independent of detector label."""
    return f"{symbol}|{direction.value}"


def _dedup_phase(state: SignalState) -> str:
    if state in WAITING_STATES:
        return "WAITING_ENTRY"
    if state in ACTIVE_STATES:
        return "ENTRY_TRIGGERED"
    if state in STOP_STATES:
        return "SL_HIT"
    return state.value


def signal_fingerprint(signal: Signal) -> str:
    risk = max(signal.trade.risk_per_unit, 1e-12)
    normalized_entry = round(signal.trade.preferred_entry / risk, 1)
    raw = f"{signal_key(signal.symbol, signal.direction)}|{normalized_entry}|{signal.score // 5}|{_dedup_phase(signal.state)}"
    return sha256(raw.encode()).hexdigest()[:20]


def _entry_touch_price(signal: Signal, close: float) -> float:
    return min(max(close, signal.trade.entry_zone_low), signal.trade.entry_zone_high)


def _invalidation_level(signal: Signal) -> float:
    return signal.trade.invalidation_level or signal.trade.stop_loss


class SignalStore:
    def __init__(self, max_size: int = 128) -> None:
        self._signals: OrderedDict[str, Signal] = OrderedDict()
        self._fingerprints: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def restore(self, signal: Signal) -> None:
        """Restore one persisted open thesis without treating it as a new alert."""
        key = signal_key(signal.symbol, signal.direction)
        self._signals[key] = signal
        self._signals.move_to_end(key)
        self._fingerprints[signal_fingerprint(signal)] = None
        self._trim()

    def should_publish(self, signal: Signal) -> bool:
        fingerprint = signal_fingerprint(signal)
        if fingerprint in self._fingerprints:
            return False
        key = signal_key(signal.symbol, signal.direction)
        previous = self._signals.get(key)
        if previous:
            if previous.state in ACTIVE_STATES | frozenset({SignalState.TP1_HIT}) and signal.state in WAITING_STATES:
                return False
            entry_change = abs(signal.trade.preferred_entry - previous.trade.preferred_entry) / max(
                previous.trade.risk_per_unit, 1e-12
            )
            meaningful = (
                _dedup_phase(signal.state) != _dedup_phase(previous.state)
                or signal.score >= previous.score + 5
                or entry_change >= 0.5
            )
            if not meaningful:
                return False
        self._signals[key] = signal
        self._signals.move_to_end(key)
        self._fingerprints[fingerprint] = None
        self._trim()
        return True

    def _trim(self) -> None:
        while len(self._signals) > self._max_size:
            self._signals.popitem(last=False)
        while len(self._fingerprints) > self._max_size * 2:
            self._fingerprints.popitem(last=False)

    def open_symbols(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(signal.symbol for signal in self._signals.values() if signal.state in OPEN_STATES))

    def expire_due(self, observed_at: datetime | None = None) -> list[Signal]:
        """Expire waiting setups without requiring a successful market request."""
        now = observed_at or datetime.now(UTC)
        events: list[Signal] = []
        for key, signal in list(self._signals.items()):
            if (
                signal.state not in WAITING_STATES
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
            self._signals[key] = updated
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
    ) -> list[Signal]:
        events: list[Signal] = []
        for timestamp, high, low, close in zip(timestamps_ms, highs, lows, closes, strict=True):
            event_at = datetime.fromtimestamp((int(timestamp) + timeframe_ms) / 1000, UTC)
            events.extend(self._track_observation(symbol, float(high), float(low), float(close), event_at))
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
    ) -> list[Signal]:
        events: list[Signal] = []
        for key, signal in list(self._signals.items()):
            if signal.symbol != symbol or signal.state not in OPEN_STATES:
                continue
            if signal.state_changed_at is not None and event_at <= signal.state_changed_at:
                continue
            observed_high = high
            observed_low = low
            if connect_previous and signal.current_price is not None:
                observed_high = max(observed_high, signal.current_price)
                observed_low = min(observed_low, signal.current_price)
            long = signal.direction is Direction.LONG
            invalidation_level = _invalidation_level(signal)
            invalidation_breached = (long and observed_low <= invalidation_level) or (
                not long and observed_high >= invalidation_level
            )
            stop_breached = (long and observed_low <= signal.trade.stop_loss) or (
                not long and observed_high >= signal.trade.stop_loss
            )

            if signal.state in WAITING_STATES:
                if signal.expires_at is not None and event_at >= signal.expires_at:
                    updated = transition(
                        signal,
                        SignalState.EXPIRED,
                        current_price=close,
                        changed_at=event_at,
                        reason="Setup validity window elapsed before the entry zone was triggered.",
                    )
                else:
                    entry_touched = (
                        observed_low <= signal.trade.entry_zone_high
                        and observed_high >= signal.trade.entry_zone_low
                    )
                    if entry_touched:
                        active_state = (
                            SignalState.ACTIVE
                            if signal.state is SignalState.CONFIRMED
                            else SignalState.ENTRY_TRIGGERED
                        )
                        trigger_price = _entry_touch_price(signal, close)
                        updated = transition(
                            signal,
                            active_state,
                            current_price=close,
                            changed_at=event_at,
                            trigger_price=trigger_price,
                            reason="Price entered the configured entry zone while the setup was valid.",
                        )
                        self._signals[key] = updated
                        events.append(updated)
                        # Entry and invalidation within one OHLC observation have
                        # unknowable ordering. Preserve the existing conservative
                        # accounting rule: activated first, then stopped.
                        if stop_breached or invalidation_breached:
                            stop_state = (
                                SignalState.STOPPED
                                if active_state is SignalState.ACTIVE
                                else SignalState.SL_HIT
                            )
                            updated = transition(
                                updated,
                                stop_state,
                                current_price=signal.trade.stop_loss,
                                changed_at=event_at,
                                reason="Stop/invalidation level was reached after the modeled entry trigger.",
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
                            reason=(
                                f"Price broke {relation} the setup invalidation level "
                                f"{invalidation_level:.8g} before entry."
                            ),
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
                        if connect_previous:
                            self._signals[key] = replace(signal, current_price=close)
                        continue
            elif stop_breached:
                stop_state = (
                    SignalState.STOPPED
                    if signal.state is SignalState.ACTIVE
                    or (signal.state is SignalState.TP1_HIT and signal.entry_trigger_price is None)
                    else SignalState.SL_HIT
                )
                updated = transition(
                    signal,
                    stop_state,
                    current_price=signal.trade.stop_loss,
                    changed_at=event_at,
                    reason="The active trade reached its structural stop level.",
                )
            elif (long and observed_high >= signal.trade.tp2) or (
                not long and observed_low <= signal.trade.tp2
            ):
                updated = transition(
                    signal,
                    SignalState.TP2_HIT,
                    current_price=signal.trade.tp2,
                    changed_at=event_at,
                    reason="The active trade reached TP2.",
                )
            elif signal.state in ACTIVE_STATES and (
                (long and observed_high >= signal.trade.tp1)
                or (not long and observed_low <= signal.trade.tp1)
            ):
                updated = transition(
                    signal,
                    SignalState.TP1_HIT,
                    current_price=signal.trade.tp1,
                    changed_at=event_at,
                    reason="The active trade reached TP1; a win is now recorded.",
                )
            else:
                if connect_previous:
                    self._signals[key] = replace(signal, current_price=close)
                continue
            self._signals[key] = updated
            events.append(updated)
        return events

    def __len__(self) -> int:
        return len(self._signals)
