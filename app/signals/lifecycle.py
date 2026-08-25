from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
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


def transition(signal: Signal, target: SignalState) -> Signal:
    if target not in ALLOWED_TRANSITIONS[signal.state]:
        raise ValueError(f"invalid signal transition {signal.state.value} -> {target.value}")
    return replace(signal, state=target)


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
        while len(self._signals) > self._max_size:
            self._signals.popitem(last=False)
        while len(self._fingerprints) > self._max_size * 2:
            self._fingerprints.popitem(last=False)
        return True

    def track_price(self, symbol: str, price: float) -> list[Signal]:
        events: list[Signal] = []
        for key, signal in list(self._signals.items()):
            if signal.symbol != symbol or signal.state not in {SignalState.CONFIRMED, SignalState.ACTIVE, SignalState.TP1_HIT}:
                continue
            long = signal.direction is Direction.LONG
            stop_breached = (long and price <= signal.trade.stop_loss) or (not long and price >= signal.trade.stop_loss)

            # A confirmed setup is not a trade until price is observed inside its
            # entry zone. Pre-entry failure invalidates the thesis; it must never
            # be recorded as a stopped position or credited with a target hit.
            if signal.state is SignalState.CONFIRMED:
                if signal.trade.entry_zone_low <= price <= signal.trade.entry_zone_high:
                    updated = transition(signal, SignalState.ACTIVE)
                elif stop_breached:
                    updated = transition(signal, SignalState.INVALIDATED)
                else:
                    continue
            elif stop_breached:
                updated = transition(signal, SignalState.STOPPED)
            elif (long and price >= signal.trade.tp2) or (not long and price <= signal.trade.tp2):
                updated = transition(signal, SignalState.TP2_HIT)
            elif signal.state is SignalState.ACTIVE and ((long and price >= signal.trade.tp1) or (not long and price <= signal.trade.tp1)):
                updated = transition(signal, SignalState.TP1_HIT)
            else:
                continue
            self._signals[key] = updated
            events.append(updated)
        return events

    def __len__(self) -> int:
        return len(self._signals)
