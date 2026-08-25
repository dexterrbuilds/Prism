from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256

from app.models import Direction, Signal, SignalState

ALLOWED_TRANSITIONS: dict[SignalState, frozenset[SignalState]] = {
    SignalState.DETECTED: frozenset({SignalState.WATCHING, SignalState.CONFIRMED, SignalState.INVALIDATED, SignalState.EXPIRED}),
    SignalState.WATCHING: frozenset({SignalState.CONFIRMED, SignalState.INVALIDATED, SignalState.EXPIRED}),
    SignalState.CONFIRMED: frozenset({SignalState.ACTIVE, SignalState.INVALIDATED, SignalState.EXPIRED}),
    SignalState.ACTIVE: frozenset({SignalState.TP1_HIT, SignalState.TP2_HIT, SignalState.STOPPED, SignalState.INVALIDATED, SignalState.EXPIRED}),
    SignalState.TP1_HIT: frozenset({SignalState.TP2_HIT, SignalState.STOPPED, SignalState.INVALIDATED, SignalState.EXPIRED}),
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
) -> Signal:
    if target not in ALLOWED_TRANSITIONS[signal.state]:
        raise ValueError(f"invalid signal transition {signal.state.value} -> {target.value}")
    event_at = changed_at or datetime.now(UTC)
    return replace(
        signal,
        state=target,
        current_price=signal.current_price if current_price is None else current_price,
        state_changed_at=event_at,
        activated_at=event_at if target is SignalState.ACTIVE and signal.activated_at is None else signal.activated_at,
        tp1_hit_at=(
            event_at
            if target in {SignalState.TP1_HIT, SignalState.TP2_HIT} and signal.tp1_hit_at is None
            else signal.tp1_hit_at
        ),
    )


def signal_key(symbol: str, direction: Direction) -> str:
    """One active directional thesis per symbol, independent of detector label."""
    return f"{symbol}|{direction.value}"


def signal_fingerprint(signal: Signal) -> str:
    risk = max(signal.trade.risk_per_unit, 1e-12)
    normalized_entry = round(signal.trade.preferred_entry / risk, 1)
    raw = f"{signal_key(signal.symbol, signal.direction)}|{normalized_entry}|{signal.score // 5}|{signal.state.value}"
    return sha256(raw.encode()).hexdigest()[:20]


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
            entry_change = abs(signal.trade.preferred_entry - previous.trade.preferred_entry) / max(previous.trade.risk_per_unit, 1e-12)
            meaningful = signal.state != previous.state or signal.score >= previous.score + 5 or entry_change >= 0.5
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

    def track_price(self, symbol: str, price: float) -> list[Signal]:
        return self._track_observation(symbol, price, price, price, datetime.now(UTC))

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

    def _track_observation(self, symbol: str, high: float, low: float, close: float, event_at: datetime) -> list[Signal]:
        events: list[Signal] = []
        for key, signal in list(self._signals.items()):
            if signal.symbol != symbol or signal.state not in {SignalState.CONFIRMED, SignalState.ACTIVE, SignalState.TP1_HIT}:
                continue
            if signal.state_changed_at is not None and event_at <= signal.state_changed_at:
                continue
            long = signal.direction is Direction.LONG
            stop_breached = (long and low <= signal.trade.stop_loss) or (not long and high >= signal.trade.stop_loss)

            # A confirmed setup is not a trade until price is observed inside its
            # entry zone. Pre-entry failure invalidates the thesis; it must never
            # be recorded as a stopped position or credited with a target hit.
            if signal.state is SignalState.CONFIRMED:
                entry_touched = low <= signal.trade.entry_zone_high and high >= signal.trade.entry_zone_low
                if entry_touched:
                    updated = transition(signal, SignalState.ACTIVE, current_price=close, changed_at=event_at)
                    self._signals[key] = updated
                    events.append(updated)
                    # When entry and stop share a candle, sequence is unknowable.
                    # Record the conservative outcome: filled, then stopped.
                    if stop_breached:
                        updated = transition(
                            updated,
                            SignalState.STOPPED,
                            current_price=signal.trade.stop_loss,
                            changed_at=event_at,
                        )
                    else:
                        continue
                elif stop_breached:
                    updated = transition(
                        signal,
                        SignalState.INVALIDATED,
                        current_price=signal.trade.stop_loss,
                        changed_at=event_at,
                    )
                else:
                    continue
            elif stop_breached:
                updated = transition(
                    signal,
                    SignalState.STOPPED,
                    current_price=signal.trade.stop_loss,
                    changed_at=event_at,
                )
            elif (long and high >= signal.trade.tp2) or (not long and low <= signal.trade.tp2):
                updated = transition(
                    signal,
                    SignalState.TP2_HIT,
                    current_price=signal.trade.tp2,
                    changed_at=event_at,
                )
            elif signal.state is SignalState.ACTIVE and (
                (long and high >= signal.trade.tp1) or (not long and low <= signal.trade.tp1)
            ):
                updated = transition(
                    signal,
                    SignalState.TP1_HIT,
                    current_price=signal.trade.tp1,
                    changed_at=event_at,
                )
            else:
                continue
            self._signals[key] = updated
            events.append(updated)
        return events

    def __len__(self) -> int:
        return len(self._signals)
