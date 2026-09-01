from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.analysis.data_quality import TIMEFRAME_MS
from app.models import CandleSeries, Direction, Signal, SignalState
from app.signals.lifecycle import (
    ACTIVE_STATES,
    READY_STATES,
    TERMINAL_STATES,
    WAITING_STATES,
    SignalStore,
)
from app.signals.repository import OutcomeRepository

logger = logging.getLogger(__name__)

PAGE_LIMIT = 250
FINER_TIMEFRAME = {"4h": "1h", "1h": "15m", "15m": "5m", "5m": "1m"}


class HistoricalMarketData(Protocol):
    async def fetch_ohlcv_page(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        as_of_ms: int,
        *,
        limit: int = PAGE_LIMIT,
    ) -> CandleSeries: ...


@dataclass(frozen=True, slots=True)
class HistoricalCandle:
    timestamp_ms: int
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    signal_id: str
    initial_state: SignalState
    final_state: SignalState
    started_at: datetime
    ended_at: datetime
    pages_fetched: int
    candles_replayed: int
    finer_pages_fetched: int
    transitions: tuple[str, ...]
    applied: bool
    modified: bool
    message: str


def _start_time(signal: Signal) -> datetime:
    if signal.state is SignalState.TP1_HIT and signal.tp1_hit_at is not None:
        return signal.tp1_hit_at
    if signal.state in ACTIVE_STATES and signal.activated_at is not None:
        return signal.activated_at
    return signal.created_at


def _invalidation(signal: Signal) -> float:
    return signal.trade.invalidation_level or signal.trade.stop_loss


def _touches_outcome_bounds(signal: Signal, candle: HistoricalCandle) -> bool:
    long = signal.direction is Direction.LONG
    stop = (long and candle.low <= signal.trade.stop_loss) or (
        not long and candle.high >= signal.trade.stop_loss
    )
    tp1 = (long and candle.high >= signal.trade.tp1) or (
        not long and candle.low <= signal.trade.tp1
    )
    tp2 = (long and candle.high >= signal.trade.tp2) or (
        not long and candle.low <= signal.trade.tp2
    )
    if signal.state in ACTIVE_STATES:
        return stop and (tp1 or tp2)
    if signal.state is SignalState.TP1_HIT:
        return stop and tp2
    if signal.state in WAITING_STATES | READY_STATES:
        entry = (
            candle.low <= signal.trade.entry_zone_high
            and candle.high >= signal.trade.entry_zone_low
        )
        invalidated = (long and candle.low <= _invalidation(signal)) or (
            not long and candle.high >= _invalidation(signal)
        )
        # Entry/outcome ordering is also resolved with finer data when possible.
        return entry and (stop or invalidated or tp1 or tp2)
    return False


def _candles(series: CandleSeries, start_ms: int, end_ms: int) -> tuple[HistoricalCandle, ...]:
    return tuple(
        HistoricalCandle(int(timestamp), float(high), float(low), float(close))
        for timestamp, high, low, close in zip(
            series.timestamp,
            series.high,
            series.low,
            series.close,
            strict=True,
        )
        if start_ms <= int(timestamp) and int(timestamp) + TIMEFRAME_MS[series.timeframe] <= end_ms
    )


class HistoricalSignalReconciler:
    """Explicit, bounded, one-signal historical lifecycle replay utility."""

    def __init__(self, repository: OutcomeRepository, market_data: HistoricalMarketData) -> None:
        self._repository = repository
        self._market_data = market_data
        self._pages_fetched = 0
        self._finer_pages_fetched = 0
        self._candles_replayed = 0

    async def reconcile(
        self,
        signal_id: str,
        *,
        lookback_hours: float,
        apply: bool = False,
        now: datetime | None = None,
    ) -> ReconciliationReport:
        if not signal_id.strip():
            raise ValueError("an explicit signal_id is required")
        if lookback_hours <= 0 or lookback_hours > 24 * 366 * 5:
            raise ValueError("lookback_hours must be greater than 0 and no more than five years")
        self._pages_fetched = 0
        self._finer_pages_fetched = 0
        self._candles_replayed = 0
        persisted = await self._repository.load_signal(signal_id)
        if persisted is None:
            raise LookupError(f"signal_id not found: {signal_id}")

        start = _start_time(persisted).astimezone(UTC)
        requested_end = start + timedelta(hours=lookback_hours)
        end = min((now or datetime.now(UTC)).astimezone(UTC), requested_end)
        if end <= start:
            raise ValueError("the requested reconciliation window contains no closed candles")
        if persisted.state in TERMINAL_STATES:
            return ReconciliationReport(
                signal_id,
                persisted.state,
                persisted.state,
                start,
                end,
                0,
                0,
                0,
                (),
                apply,
                False,
                "Signal is already terminal; no historical state was changed.",
            )

        # The maintenance replay intentionally ignores a later observation
        # cursor.  It starts from the exact signal's creation/activation point
        # without changing the persisted row unless --apply is supplied.
        replay = replace(persisted, last_evaluated_at=start - timedelta(microseconds=1))
        store = SignalStore(max_size=1)
        store.restore(replay)
        transition_names: list[str] = []
        cursor_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        timeframe = replay.trading_timeframe
        interval_ms = TIMEFRAME_MS.get(timeframe)
        if interval_ms is None:
            raise ValueError(f"unsupported execution timeframe: {timeframe}")

        while cursor_ms < end_ms:
            page = await self._market_data.fetch_ohlcv_page(
                replay.symbol,
                timeframe,
                cursor_ms,
                end_ms,
                limit=PAGE_LIMIT,
            )
            self._pages_fetched += 1
            page_candles = _candles(page, cursor_ms, end_ms)
            if not page_candles:
                break
            for candle in page_candles:
                current = store.get(signal_id)
                if current is None or current.state in TERMINAL_STATES:
                    break
                events = await self._replay_candle(store, signal_id, timeframe, candle)
                self._candles_replayed += 1
                for event in events:
                    transition_names.append(event.state.value)
                    logger.info(
                        "historical_signal_event_recovered signal_id=%s symbol=%s state=%s at=%s price=%s apply=%s",
                        event.id,
                        event.symbol,
                        event.state.value,
                        event.state_changed_at.isoformat() if event.state_changed_at else "unknown",
                        event.current_price,
                        apply,
                    )
                    if apply:
                        inserted = await self._repository.record_event(event)
                        if not inserted:
                            logger.info(
                                "historical_signal_event_idempotent signal_id=%s state=%s",
                                event.id,
                                event.state.value,
                            )
                if store.get(signal_id) is not None and store.get(signal_id).state in TERMINAL_STATES:  # type: ignore[union-attr]
                    break
            last_timestamp = page_candles[-1].timestamp_ms
            next_cursor = last_timestamp + interval_ms
            if next_cursor <= cursor_ms:
                logger.warning("historical_reconciliation_no_progress signal_id=%s", signal_id)
                break
            cursor_ms = next_cursor
            current = store.get(signal_id)
            if current is None or current.state in TERMINAL_STATES:
                break
            if len(page_candles) < PAGE_LIMIT:
                break

        final = store.get(signal_id)
        assert final is not None
        modified = bool(transition_names)
        if apply and final.state not in TERMINAL_STATES:
            # Persist the forward-only evaluation cursor/MAE/MFE only for this
            # signal.  Never regress a newer cursor established by runtime.
            original_cursor = persisted.last_evaluated_at
            if original_cursor is not None and (
                final.last_evaluated_at is None or final.last_evaluated_at < original_cursor
            ):
                final = replace(final, last_evaluated_at=original_cursor)
            await self._repository.record_observation(final)

        return ReconciliationReport(
            signal_id=signal_id,
            initial_state=persisted.state,
            final_state=final.state,
            started_at=start,
            ended_at=end,
            pages_fetched=self._pages_fetched,
            candles_replayed=self._candles_replayed,
            finer_pages_fetched=self._finer_pages_fetched,
            transitions=tuple(transition_names),
            applied=apply,
            modified=modified and apply,
            message=(
                "Recovered lifecycle transitions were persisted."
                if modified and apply
                else "Recovered lifecycle transitions were previewed only."
                if modified
                else "No lifecycle transition was proven in the requested window."
            ),
        )

    async def _replay_candle(
        self,
        store: SignalStore,
        signal_id: str,
        timeframe: str,
        candle: HistoricalCandle,
    ) -> list[Signal]:
        current = store.get(signal_id)
        if current is None or current.state in TERMINAL_STATES:
            return []
        finer = FINER_TIMEFRAME.get(timeframe)
        if finer is not None and _touches_outcome_bounds(current, candle):
            finer_candles = await self._complete_finer_candles(current.symbol, finer, timeframe, candle)
            if finer_candles is not None:
                events: list[Signal] = []
                for finer_candle in finer_candles:
                    events.extend(await self._replay_candle(store, signal_id, finer, finer_candle))
                return events
        interval_ms = TIMEFRAME_MS[timeframe]
        return store.track_candles(
            current.symbol,
            (candle.timestamp_ms,),
            (candle.high,),
            (candle.low,),
            (candle.close,),
            timeframe_ms=interval_ms,
        )

    async def _complete_finer_candles(
        self,
        symbol: str,
        finer_timeframe: str,
        parent_timeframe: str,
        candle: HistoricalCandle,
    ) -> Sequence[HistoricalCandle] | None:
        start_ms = candle.timestamp_ms
        end_ms = start_ms + TIMEFRAME_MS[parent_timeframe]
        finer_ms = TIMEFRAME_MS[finer_timeframe]
        expected = TIMEFRAME_MS[parent_timeframe] // finer_ms
        try:
            page = await self._market_data.fetch_ohlcv_page(
                symbol,
                finer_timeframe,
                start_ms,
                end_ms,
                limit=min(PAGE_LIMIT, expected + 2),
            )
        except Exception as exc:
            logger.warning(
                "historical_finer_fetch_failure symbol=%s timeframe=%s error=%s",
                symbol,
                finer_timeframe,
                type(exc).__name__,
            )
            return None
        self._finer_pages_fetched += 1
        candles = _candles(page, start_ms, end_ms)
        timestamps = [item.timestamp_ms for item in candles]
        expected_timestamps = list(range(start_ms, end_ms, finer_ms))
        if timestamps != expected_timestamps:
            logger.warning(
                "historical_finer_data_incomplete symbol=%s timeframe=%s expected=%d received=%d",
                symbol,
                finer_timeframe,
                len(expected_timestamps),
                len(timestamps),
            )
            return None
        return candles
