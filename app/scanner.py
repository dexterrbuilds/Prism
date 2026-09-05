from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from app.ai.analyst import AIReviewService
from app.analysis.bias import derive_directional_bias
from app.analysis.context import AnalysisContext, analyze_snapshot
from app.analysis.data_quality import validate_candles
from app.api.health import RuntimeHealth
from app.config import Settings
from app.exchange.client import ExchangeClient
from app.models import (
    AIReviewVerdict,
    CandleSeries,
    ConfluenceEvidence,
    Direction,
    MarketSnapshot,
    PublicationState,
    RejectionReason,
    SetupCandidate,
    Signal,
    SignalMode,
    SignalState,
)
from app.signals.entry_plan import calculate_entry_plan
from app.signals.entry_quality import score_entry_quality, two_gate_result
from app.signals.lifecycle import (
    TERMINAL_STATES,
    SignalStore,
    build_setup_fingerprint,
    create_signal_id,
    transition,
)
from app.signals.repository import OutcomeRepository
from app.signals.risk import RiskPlanningError, build_trade_plan
from app.signals.scoring import score_candidate
from app.signals.validator import validate_candidate
from app.signals.validity import derive_setup_validity_minutes
from app.strategies import detect_setups
from app.strategies.counter import evaluate_counter_setup
from app.telegram.bot import TelegramPublishResult, TelegramService
from app.telegram.chart import render_signal_chart
from app.telegram.pnl_card import render_pnl_card

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SymbolScanResult:
    success: bool
    error: str | None = None


def _setup_origin(candidate: SetupCandidate, context: AnalysisContext, primary_tf: str) -> tuple[float, int | None]:
    """Return the nearest confirmed structural anchor and its stable candle timestamp."""
    analysis = context.timeframes[primary_tf]
    candles = context.snapshot.series[primary_tf]
    midpoint = (candidate.ideal_entry_low + candidate.ideal_entry_high) / 2
    anchors: list[tuple[float, float, int]] = []
    for event in analysis.structure.events:
        if event.direction is candidate.direction and 0 <= event.index < len(candles):
            anchors.append((abs(event.level - midpoint), event.level, int(candles.timestamp[event.index])))
    for swing in analysis.structure.swings:
        if 0 <= swing.index < len(candles):
            anchors.append((abs(swing.price - midpoint), swing.price, int(candles.timestamp[swing.index])))
    for zone in analysis.zones:
        if 0 <= zone.last_index < len(candles):
            anchors.append((abs(zone.midpoint - midpoint), zone.midpoint, int(candles.timestamp[zone.last_index])))
    if not anchors:
        return midpoint, None
    _, level, timestamp = min(anchors, key=lambda item: item[0])
    return level, timestamp


def select_best_signal(signals: list[Signal], ambiguity_buffer: int = 5) -> Signal | None:
    """Return one ranked thesis; reject near-tied opposite directions."""
    if not signals:
        return None
    state_priority = {
        SignalState.ENTRY_TRIGGERED: 4,
        SignalState.ACTIVE: 4,
        SignalState.ENTRY_READY: 3,
        SignalState.WAITING_FOR_ENTRY: 2,
        SignalState.WAITING_ENTRY: 3,
        SignalState.CONFIRMED: 3,
        SignalState.CREATED: 2,
        SignalState.WATCHING: 1,
    }
    ordered = sorted(
        signals,
        key=lambda signal: (
            1 if signal.entry_quality and signal.entry_quality.total >= 75 and not signal.entry_quality.hard_reasons else 0,
            signal.entry_quality.total if signal.entry_quality else 0,
            signal.score,
            state_priority.get(signal.state, 0),
        ),
        reverse=True,
    )
    best_by_direction: dict[Direction, Signal] = {}
    for signal in ordered:
        best_by_direction.setdefault(signal.direction, signal)
    if len(best_by_direction) > 1:
        directional_best = sorted(best_by_direction.values(), key=lambda signal: signal.score, reverse=True)
        if directional_best[0].score - directional_best[1].score <= ambiguity_buffer:
            return None
    winner = ordered[0]
    supporting = tuple(
        dict.fromkeys(
            signal.strategy
            for signal in ordered[1:]
            if signal.direction is winner.direction
            and signal.strategy != winner.strategy
            and abs(signal.trade.preferred_entry - winner.trade.preferred_entry)
            <= max(signal.trade.risk_per_unit, winner.trade.risk_per_unit) * 0.75
        )
    )[:3]
    return replace(winner, supporting_strategies=supporting)


class Scanner:
    def __init__(
        self,
        settings: Settings,
        exchange: ExchangeClient,
        telegram: TelegramService,
        health: RuntimeHealth,
        outcomes: OutcomeRepository | None = None,
        ai_reviews: AIReviewService | None = None,
    ) -> None:
        self.settings = settings
        self.exchange = exchange
        self.telegram = telegram
        self.health = health
        self.outcomes = outcomes
        self.ai_reviews = ai_reviews
        self.store = SignalStore(max_size=128)
        self._manual_scan_event = asyncio.Event()

    def _initial_publication_succeeded(self, delivered: set[str]) -> bool:
        if self.settings.dry_run:
            return True
        channel_success = bool(delivered.intersection(self.settings.telegram_channel_ids))
        primary = self.settings.telegram_chat_ids[0] if self.settings.telegram_chat_ids else None
        return channel_success or (primary is not None and primary in delivered)

    async def _ensure_initial_publication(
        self,
        signal: Signal,
        *,
        chart_png: bytes | None = None,
    ) -> Signal:
        """Publish/retry one immutable signal and persist delivery separately from lifecycle."""
        intended = signal.intended_destination_ids or self.settings.telegram_delivery_ids
        delivered_before = set(signal.delivered_destination_ids)
        pending = tuple(destination for destination in intended if destination not in delivered_before)
        if signal.publication_state is PublicationState.PUBLISHED and not pending:
            return signal
        logger.info(
            "publication_path signal_id=%s stage=TELEGRAM_PUBLISH_START state=%s pending_destinations=%d attempt=%d",
            signal.id,
            signal.publication_state.value,
            len(pending),
            signal.publish_attempts + 1,
        )
        raw_result = await self.telegram.publish(signal, chart_png=chart_png, destinations=pending)
        if isinstance(raw_result, TelegramPublishResult):
            delivered_now = set(raw_result.delivered_destination_ids)
            errors = raw_result.errors
            message_id_for = raw_result.message_id_for
        else:
            # Test doubles and older adapters may still return bool.
            delivered_now = set(pending) if raw_result else set()
            errors = () if raw_result else ("Telegram adapter returned failure",)
            message_id_for = lambda destination: None  # noqa: E731
        delivered = delivered_before | delivered_now
        now = datetime.now(UTC)
        channel_successes = delivered.intersection(self.settings.telegram_channel_ids)
        dm_successes = delivered.intersection(self.settings.telegram_chat_ids)
        attempted_dms = set(pending).intersection(self.settings.telegram_chat_ids)
        channel_message_id = signal.channel_message_id
        if channel_message_id is None:
            for destination in self.settings.telegram_channel_ids:
                if destination in delivered_now:
                    channel_message_id = message_id_for(destination)
                    break
        initial_success = self._initial_publication_succeeded(delivered)
        state = PublicationState.PUBLISHED if initial_success else PublicationState.PUBLISH_FAILED
        failed_dms = set(intended).intersection(self.settings.telegram_chat_ids) - dm_successes
        updated = replace(
            signal,
            publication_state=state,
            published_at=signal.published_at or (now if initial_success else None),
            channel_published_at=(
                signal.channel_published_at or (now if channel_successes else None)
            ),
            channel_message_id=channel_message_id,
            dm_delivery_attempted_at=(now if attempted_dms else signal.dm_delivery_attempted_at),
            dm_success_count=len(dm_successes),
            dm_failure_count=len(failed_dms),
            publish_attempts=signal.publish_attempts + 1,
            last_publish_error="; ".join(errors)[:500] if errors else None,
            intended_destination_ids=intended,
            delivered_destination_ids=tuple(destination for destination in intended if destination in delivered),
        )
        self.store.restore(updated)
        publication_persisted = True
        if (
            self.outcomes is not None
            and (not self.settings.dry_run or self.settings.dry_run_track_outcomes)
            and not await self.outcomes.record_publication(updated)
        ):
            publication_persisted = False
            logger.error(
                "publication_path signal_id=%s stage=PUBLISH_RESULT_PERSIST_FAILED state=%s",
                signal.id,
                state.value,
            )
        if publication_persisted:
            logger.info(
                "publication_path signal_id=%s stage=PUBLISH_RESULT_PERSISTED state=%s",
                signal.id,
                state.value,
            )
        logger.info(
            "publication_path signal_id=%s stage=CHANNEL_SEND_RESULT success=%s message_id=%s",
            signal.id,
            bool(channel_successes),
            channel_message_id or "none",
        )
        logger.info(
            "publication_path signal_id=%s stage=DM_FANOUT_RESULT sent=%d failed=%d",
            signal.id,
            len(dm_successes),
            len(failed_dms),
        )
        if initial_success:
            if signal.published_at is None:
                self.health.publication_successes += 1
            logger.info(
                "publication_path signal_id=%s stage=SIGNAL_PUBLISHED delivered=%d",
                signal.id,
                len(delivered),
            )
        else:
            self.health.publication_failures += 1
            logger.warning(
                "publication_path signal_id=%s stage=PUBLISH_FAILED errors=%s",
                signal.id,
                updated.last_publish_error or "no primary/channel destination accepted the message",
            )
        self.health.dm_delivery_failures += len(failed_dms)
        return updated

    async def recover_unpublished_signals(self) -> int:
        """Retry still-actionable initial publications and incomplete DM fan-out."""
        recovered = 0
        now = datetime.now(UTC)
        for expired in self.store.expire_due(now):
            await self._publish_lifecycle_events([expired], expired.symbol)
        for signal in self.store.open_signals():
            if signal.publication_state not in {
                PublicationState.PUBLISH_PENDING,
                PublicationState.PUBLISH_FAILED,
                PublicationState.PUBLISHED,
            }:
                continue
            if signal.expires_at is not None and now >= signal.expires_at and signal.activated_at is None:
                continue
            before = signal.publication_state
            updated = await self._ensure_initial_publication(signal)
            if before is not PublicationState.PUBLISHED and updated.publication_state is PublicationState.PUBLISHED:
                recovered += 1
                logger.info("publication_path signal_id=%s stage=STARTUP_PUBLICATION_RECOVERED", signal.id)
        return recovered

    async def restore_outcomes(self) -> None:
        """Restore persisted open theses before the first market scan."""
        if self.outcomes is None:
            return
        restored = await self.outcomes.load_open_signals()
        for signal in restored:
            self.store.restore(signal)
        logger.info(
            "signal_outcomes_restored count=%d unique_symbols=%d signal_ids=%s",
            len(restored),
            len({signal.symbol for signal in restored}),
            ",".join(signal.id for signal in restored[:12]),
        )

    async def reconcile_open_signals(self, *, startup: bool = False) -> int:
        """Replay closed execution candles for every persisted non-terminal instance."""
        signals = self.store.open_signals()
        pairs = tuple(dict.fromkeys((signal.symbol, signal.trading_timeframe) for signal in signals))
        if not pairs:
            return 0
        as_of_ms = int(time.time() * 1000)
        fetched = await asyncio.gather(
            *(self.exchange.fetch_ohlcv(symbol, timeframe, as_of_ms) for symbol, timeframe in pairs),
            return_exceptions=True,
        )
        reconciled_events = 0
        reconciled_ids: set[str] = set()
        for (symbol, timeframe), result in zip(pairs, fetched, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "signal_reconciliation_failure symbol=%s timeframe=%s error=%s",
                    symbol,
                    timeframe,
                    type(result).__name__,
                )
                continue
            quality = validate_candles(result)
            if not quality.valid:
                logger.warning(
                    "signal_reconciliation_failure symbol=%s timeframe=%s reason=data_quality",
                    symbol,
                    timeframe,
                )
                continue
            timeframe_ms = 300_000 if timeframe == "5m" else 900_000
            events = self.store.track_candles(
                symbol,
                result.timestamp,
                result.high,
                result.low,
                result.close,
                timeframe_ms=timeframe_ms,
                trading_timeframe=timeframe,
            )
            if events:
                reconciled_events += len(events)
                reconciled_ids.update(event.id for event in events)
                await self._publish_lifecycle_events(events, symbol)
            if self.outcomes is not None and (not self.settings.dry_run or self.settings.dry_run_track_outcomes):
                for tracked in self.store.signals_for_symbol(symbol):
                    await self.outcomes.record_observation(tracked)
        if startup and reconciled_ids:
            self.health.orphaned_signals_reconciled += len(reconciled_ids)
        logger.info(
            "signal_reconciliation_completed startup=%s instances=%d reconciled_signals=%d transitions=%d",
            startup,
            len(signals),
            len(reconciled_ids),
            reconciled_events,
        )
        return len(reconciled_ids)

    def request_manual_scan(self) -> bool:
        """Wake the scanner once; never overlap an active watchlist scan."""
        if self.health.scanner == "running" or self._manual_scan_event.is_set():
            return False
        self._manual_scan_event.set()
        return True

    async def _wait_for_next_scan(self, stop_event: asyncio.Event) -> None:
        deadline = time.monotonic() + self.settings.scan_interval_seconds
        while not stop_event.is_set():
            timeout = min(self.settings.lifecycle_monitor_seconds, max(0.0, deadline - time.monotonic()))
            if timeout <= 0:
                return
            stop_task = asyncio.create_task(stop_event.wait())
            manual_task = asyncio.create_task(self._manual_scan_event.wait())
            done, pending = await asyncio.wait(
                {stop_task, manual_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stop_task in done:
                return
            if manual_task in done:
                self._manual_scan_event.clear()
                return
            await self._monitor_open_setups()

    async def _monitor_open_setups(self) -> None:
        observed_at = datetime.now(UTC)
        for expired in self.store.expire_due(observed_at):
            await self._publish_lifecycle_events([expired], expired.symbol)
        symbols = self.store.open_symbols()
        if not symbols:
            return
        await self._refresh_entry_readiness(observed_at)
        await self.reconcile_open_signals()
        await self.recover_unpublished_signals()
        try:
            prices = await self.exchange.fetch_prices(symbols)
        except Exception as exc:
            logger.warning("lifecycle_price_failure error=%s", type(exc).__name__)
            return
        for symbol, price in prices.items():
            events = self.store.track_price(symbol, price, observed_at=observed_at)
            await self._publish_lifecycle_events(events, symbol)
            if self.outcomes is not None and (not self.settings.dry_run or self.settings.dry_run_track_outcomes):
                for tracked in self.store.signals_for_symbol(symbol):
                    if tracked.state_changed_at == observed_at and tracked in events:
                        continue
                    await self.outcomes.record_observation(tracked)

    async def _refresh_entry_readiness(self, observed_at: datetime) -> None:
        """Re-evaluate waiting entries from closed candles; ticker touches cannot confirm structure."""
        for signal in self.store.open_signals():
            if signal.state not in {
                SignalState.BIAS_DETECTED,
                SignalState.SETUP_FORMING,
                SignalState.WAITING_FOR_ENTRY,
                SignalState.CREATED,
                SignalState.WAITING_ENTRY,
                SignalState.CONFIRMED,
            }:
                continue
            timeframes = ("4h", "1h", "15m", "5m") if signal.mode is SignalMode.SCALP else ("4h", "1h", "15m")
            as_of_ms = int(observed_at.timestamp() * 1000)
            fetched = await asyncio.gather(
                *(self.exchange.fetch_ohlcv(signal.symbol, timeframe, as_of_ms) for timeframe in timeframes),
                return_exceptions=True,
            )
            if any(isinstance(item, BaseException) for item in fetched):
                logger.warning("entry_refresh_failure symbol=%s reason=request", signal.symbol)
                continue
            series: dict[str, CandleSeries] = {}
            for timeframe, item in zip(timeframes, fetched, strict=True):
                if not isinstance(item, CandleSeries):
                    break
                series[timeframe] = item
            if len(series) != len(timeframes) or any(not validate_candles(item).valid for item in series.values()):
                logger.warning("entry_refresh_failure symbol=%s reason=data_quality", signal.symbol)
                continue
            try:
                context = analyze_snapshot(
                    MarketSnapshot(signal.symbol, series, as_of_ms),
                    self.settings.pivot_left,
                    self.settings.pivot_right,
                    self.settings.zone_atr_tolerance,
                )
                candidate = SetupCandidate(
                    symbol=signal.symbol,
                    strategy=signal.strategy,
                    direction=signal.direction,
                    timeframe=signal.analysis_timeframe,
                    detected_at_ms=int(signal.created_at.timestamp() * 1000),
                    ideal_entry_low=signal.trade.entry_zone_low,
                    ideal_entry_high=signal.trade.entry_zone_high,
                    trigger=signal.trade.trigger,
                    invalidation_level=signal.trade.invalidation_level or signal.trade.stop_loss,
                    quality=signal.score / 100.0,
                    evidence=ConfluenceEvidence(),
                    confirmed=False,
                    mode=signal.mode,
                )
                execution_tf = "5m" if signal.mode is SignalMode.SCALP else "15m"
                quality = score_entry_quality(
                    candidate,
                    context,
                    signal.trade,
                    current_price=series[execution_tf].latest_close,
                    max_chase_atr=self.settings.max_chase_atr,
                )
                minimum = self.settings.scalp_minimum_entry_score if signal.mode is SignalMode.SCALP else self.settings.minimum_entry_score
                if quality.total < minimum:
                    continue
                if self.ai_reviews is not None and self.ai_reviews.enabled:
                    review = await self.ai_reviews.review(
                        replace(signal, entry_quality=quality, current_price=series[execution_tf].latest_close)
                    )
                    if review.verdict in {AIReviewVerdict.WAIT, AIReviewVerdict.REJECT}:
                        continue
                ready = self.store.mark_entry_ready(
                    signal.id,
                    quality,
                    observed_at=observed_at,
                    current_price=series[execution_tf].latest_close,
                    minimum_score=minimum,
                )
                if ready is not None:
                    await self._publish_lifecycle_events([ready], signal.symbol)
            except (ValueError, ArithmeticError) as exc:
                logger.warning("entry_refresh_failure symbol=%s error=%s", signal.symbol, type(exc).__name__)

    async def _publish_lifecycle_events(self, events: list[Signal], symbol: str) -> None:
        for event in events:
            logger.info(
                "signal_lifecycle signal_id=%s symbol=%s strategy=%s state=%s",
                event.id,
                event.symbol,
                event.strategy,
                event.state.value,
            )
            publish_event = True
            if self.outcomes is not None and (not self.settings.dry_run or self.settings.dry_run_track_outcomes):
                publish_event = await self.outcomes.record_event(event)
            if not publish_event:
                logger.info(
                    "signal_lifecycle_duplicate_suppressed signal_id=%s symbol=%s state=%s",
                    event.id,
                    event.symbol,
                    event.state.value,
                )
                continue
            # ENTRY_READY is the first publishable state when WATCH alerts are
            # disabled.  Send the full original signal—not a contextless
            # lifecycle update—and persist success before enabling follow-ups.
            if (
                event.state is SignalState.ENTRY_READY
                and event.publication_state
                in {PublicationState.PUBLISH_PENDING, PublicationState.PUBLISH_FAILED}
            ):
                await self._ensure_initial_publication(event)
                continue
            if event.publication_state is not PublicationState.PUBLISHED or event.published_at is None:
                self.health.lifecycle_notifications_suppressed += 1
                logger.warning(
                    "lifecycle_notification_suppressed signal_id=%s state=%s publication_state=%s reason=initial_not_published",
                    event.id,
                    event.state.value,
                    event.publication_state.value,
                )
                if event.state in TERMINAL_STATES:
                    unpublished = replace(
                        event,
                        publication_state=PublicationState.UNPUBLISHED_TERMINAL,
                        last_publish_error=(
                            event.last_publish_error
                            or "Signal became terminal before its initial Telegram publication succeeded."
                        ),
                    )
                    self.store.restore(unpublished)
                    if self.outcomes is not None and (
                        not self.settings.dry_run or self.settings.dry_run_track_outcomes
                    ):
                        await self.outcomes.record_publication(unpublished)
                continue
            lifecycle_card: bytes | None = None
            should_render_pnl = event.state in {SignalState.TP1_HIT, SignalState.TP2_HIT} or (
                event.state in {SignalState.STOPPED, SignalState.SL_HIT} and event.tp1_hit_at is None
            )
            if should_render_pnl:
                try:
                    lifecycle_card = await asyncio.to_thread(render_pnl_card, event)
                except Exception as exc:
                    logger.warning("pnl_card_render_failure symbol=%s error=%s", symbol, type(exc).__name__)
            result = await self.telegram.publish(
                event,
                lifecycle=True,
                chart_png=lifecycle_card,
                destinations=event.delivered_destination_ids,
            )
            if not result and not self.settings.dry_run:
                logger.warning(
                    "lifecycle_delivery_failure signal_id=%s state=%s",
                    event.id,
                    event.state.value,
                )

    async def run(self, stop_event: asyncio.Event) -> None:
        self.health.scanner = "running"
        while not stop_event.is_set():
            started = time.monotonic()
            as_of_ms = int(time.time() * 1000)
            self.health.scanner = "running"
            logger.info("scan_started symbols=%d", len(self.settings.watchlist))
            results = await asyncio.gather(
                *(self._scan_symbol(symbol, as_of_ms) for symbol in self.settings.watchlist),
                return_exceptions=True,
            )
            failures = 0
            errors: list[str] = []
            for symbol, result in zip(self.settings.watchlist, results, strict=True):
                if isinstance(result, BaseException):
                    failures += 1
                    detail = str(result).strip()[:240]
                    error = f"{symbol}: {type(result).__name__}" + (f" - {detail}" if detail else "")
                    errors.append(error)
                    logger.error(
                        "symbol_scan_failure symbol=%s error=%s message=%s",
                        symbol,
                        type(result).__name__,
                        detail or "none",
                        exc_info=(type(result), result, result.__traceback__),
                    )
                elif not result.success:
                    failures += 1
                    if result.error:
                        errors.append(result.error)
            self.exchange.clear_cycle_cache()
            self.health.last_scan_ms = as_of_ms
            self.health.scanned_symbols = len(self.settings.watchlist) - failures
            self.health.scan_errors += failures
            self.health.last_scan_errors = failures
            self.health.last_error = errors[0] if errors else None
            self.health.scanner = "sleeping"
            logger.info("scan_completed duration_seconds=%.3f failures=%d", time.monotonic() - started, failures)
            await self._wait_for_next_scan(stop_event)
        self.health.scanner = "stopped"

    async def _scan_symbol(self, symbol: str, as_of_ms: int) -> SymbolScanResult:
        fetched = await asyncio.gather(
            *(self.exchange.fetch_ohlcv(symbol, timeframe, as_of_ms) for timeframe in self.settings.timeframes),
            return_exceptions=True,
        )
        series = {}
        for timeframe, result in zip(self.settings.timeframes, fetched, strict=True):
            if isinstance(result, BaseException):
                logger.warning("request_failure symbol=%s timeframe=%s error=%s", symbol, timeframe, type(result).__name__)
                logger.info("setup_rejected symbol=%s reason=%s", symbol, RejectionReason.DATA_QUALITY.value)
                return SymbolScanResult(False, f"{symbol} {timeframe}: {result}")
            quality = validate_candles(result)
            if not quality.valid:
                logger.info("setup_rejected symbol=%s timeframe=%s reason=%s details=%s", symbol, timeframe, RejectionReason.DATA_QUALITY.value, ",".join(quality.reasons))
                return SymbolScanResult(False, f"{symbol} {timeframe}: DATA QUALITY - {', '.join(quality.reasons)}")
            series[timeframe] = result
        snapshot = MarketSnapshot(symbol, series, as_of_ms)
        try:
            context = analyze_snapshot(snapshot, self.settings.pivot_left, self.settings.pivot_right, self.settings.zone_atr_tolerance)
        except (ValueError, ArithmeticError) as exc:
            logger.warning("indicator_failure symbol=%s error=%s", symbol, type(exc).__name__)
            return SymbolScanResult(False, f"{symbol}: indicator failure - {type(exc).__name__}")
        directional_bias = derive_directional_bias(context)
        candidates = (
            detect_setups(context, include_scalp=True)
            if self.settings.scalp_enabled
            else detect_setups(context)
        )
        for previous in self.store.signals_for_symbol(symbol):
            counter = evaluate_counter_setup(previous, context)
            if counter is not None:
                candidates.append(counter)
        self.health.candidates_detected += len(candidates)
        if not candidates:
            logger.info("setup_rejected symbol=%s reason=%s", symbol, RejectionReason.NO_SETUP.value)
        valid_signals: list[Signal] = []
        for candidate in candidates:
            created_at = datetime.fromtimestamp(as_of_ms / 1000, UTC)
            candidate_signal_id = create_signal_id(symbol, candidate.direction, created_at)
            logger.info(
                "publication_path signal_id=%s stage=CANDIDATE_DETECTED symbol=%s strategy=%s direction=%s",
                candidate_signal_id,
                symbol,
                candidate.strategy,
                candidate.direction.value,
            )
            score = score_candidate(candidate, context)
            setup_minimum = self.settings.scalp_minimum_setup_score if candidate.mode is SignalMode.SCALP else self.settings.minimum_valid_score
            if score.total < setup_minimum:
                logger.info(
                    "publication_path signal_id=%s stage=SETUP_REJECTED symbol=%s strategy=%s score=%d reason=%s",
                    candidate_signal_id,
                    symbol,
                    candidate.strategy,
                    score.total,
                    RejectionReason.LOW_CONFLUENCE.value,
                )
                continue
            logger.info(
                "publication_path signal_id=%s stage=SETUP_SCORE_PASSED score=%d minimum=%d",
                candidate_signal_id,
                score.total,
                setup_minimum,
            )
            primary_tf = "15m" if candidate.mode is SignalMode.SCALP else "1h"
            execution_tf = "5m" if candidate.mode is SignalMode.SCALP else "15m"
            entry_plan = calculate_entry_plan(candidate, context)
            try:
                plan = build_trade_plan(
                    candidate,
                    series[execution_tf].latest_close,
                    float(context.timeframes[primary_tf].indicators.atr[-1]),
                    context.timeframes[primary_tf].zones,
                    entry_plan,
                )
            except RiskPlanningError:
                logger.info(
                    "publication_path signal_id=%s stage=HARD_VALIDATION_REJECTED symbol=%s strategy=%s reason=%s",
                    candidate_signal_id,
                    symbol,
                    candidate.strategy,
                    RejectionReason.POOR_RR.value,
                )
                continue
            entry_quality = score_entry_quality(
                candidate,
                context,
                plan,
                current_price=series[execution_tf].latest_close,
                max_chase_atr=self.settings.max_chase_atr,
            )
            entry_minimum = self.settings.scalp_minimum_entry_score if candidate.mode is SignalMode.SCALP else self.settings.minimum_entry_score
            validation = validate_candidate(
                candidate,
                context,
                score,
                plan,
                self.settings.max_chase_atr,
                setup_minimum,
                entry_quality,
                entry_minimum,
            )
            if not validation.valid:
                logger.info(
                    "publication_path signal_id=%s stage=HARD_VALIDATION_REJECTED symbol=%s strategy=%s score=%d entry_score=%d reason=%s",
                    candidate_signal_id,
                    symbol,
                    candidate.strategy,
                    score.total,
                    entry_quality.total,
                    validation.reason.value if validation.reason else "UNKNOWN",
                )
                continue
            gate = two_gate_result(score.total, entry_quality, setup_minimum, entry_minimum)
            if gate.hard_reject:
                logger.info(
                    "publication_path signal_id=%s stage=ENTRY_QUALITY_REJECTED symbol=%s strategy=%s entry_score=%d reason=%s",
                    candidate_signal_id,
                    symbol,
                    candidate.strategy,
                    entry_quality.total,
                    entry_quality.hard_reasons[0],
                )
                continue
            logger.info(
                "publication_path signal_id=%s stage=HARD_VALIDATION_PASSED setup_score=%d entry_score=%d actionable=%s",
                candidate_signal_id,
                score.total,
                entry_quality.total,
                gate.actionable,
            )
            ready = gate.actionable and entry_quality.retest_completed and entry_quality.lower_timeframe_confirmed
            state = SignalState.ENTRY_READY if ready else SignalState.WAITING_FOR_ENTRY
            validity_minutes = derive_setup_validity_minutes(candidate, plan)
            expires_at = created_at + timedelta(minutes=validity_minutes)
            atr = float(context.timeframes[primary_tf].indicators.atr[-1])
            invalidation_level = plan.invalidation_level or plan.stop_loss
            relation = "above" if candidate.direction is Direction.LONG else "below"
            missed_distance = atr * self.settings.max_chase_atr
            missed_relation = "above" if candidate.direction is Direction.LONG else "below"
            missed_limit = (
                plan.entry_zone_high + missed_distance
                if candidate.direction is Direction.LONG
                else plan.entry_zone_low - missed_distance
            )
            major_structure_level, setup_origin_ms = _setup_origin(candidate, context, primary_tf)
            setup_origin_at = (
                datetime.fromtimestamp(setup_origin_ms / 1000, UTC)
                if setup_origin_ms is not None
                else None
            )
            setup_fingerprint = build_setup_fingerprint(
                symbol=symbol,
                direction=candidate.direction,
                mode=candidate.mode,
                strategy=candidate.strategy,
                regime=context.regime.value,
                entry_low=plan.entry_zone_low,
                entry_high=plan.entry_zone_high,
                invalidation=invalidation_level,
                major_structure_level=major_structure_level,
                atr=atr,
                setup_origin_ms=setup_origin_ms,
            )
            valid_conditions = (
                f"Price remains {relation} {invalidation_level:.8g}",
                f"Price does not close {missed_relation} {missed_limit:.8g} before entry",
                f"Entry triggers before {expires_at.strftime('%H:%M UTC')}",
            )
            signal = Signal(
                id=candidate_signal_id, symbol=symbol, strategy=candidate.strategy,
                direction=candidate.direction, regime=context.regime, score=score.total, grade=score.grade,
                state=state,
                trade=plan,
                evidence=score.evidence,
                created_at=created_at,
                current_price=series[execution_tf].latest_close,
                state_changed_at=created_at,
                trading_timeframe=execution_tf,
                analysis_timeframe=candidate.timeframe,
                expires_at=expires_at,
                validity_minutes=validity_minutes,
                valid_conditions=valid_conditions,
                max_missed_distance=missed_distance,
                mode=candidate.mode,
                entry_quality=entry_quality,
                atr_at_entry=float(context.timeframes[execution_tf].indicators.atr[-1]),
                directional_bias=directional_bias,
                setup_fingerprint=setup_fingerprint,
                setup_origin_at=setup_origin_at,
                major_structure_level=major_structure_level,
                last_evaluated_at=created_at,
            )
            logger.info(
                "publication_path signal_id=%s stage=SIGNAL_CREATED symbol=%s strategy=%s state=%s",
                signal.id,
                symbol,
                candidate.strategy,
                signal.state.value,
            )
            if self.ai_reviews is not None and self.ai_reviews.enabled:
                review = await self.ai_reviews.review(signal)
                signal = replace(signal, ai_review=review)
                if review.verdict is AIReviewVerdict.REJECT:
                    logger.info(
                        "publication_path signal_id=%s stage=AI_REVIEW_REJECTED symbol=%s strategy=%s",
                        signal.id,
                        symbol,
                        candidate.strategy,
                    )
                    continue
                if review.verdict is AIReviewVerdict.WAIT:
                    signal = replace(signal, state=SignalState.WAITING_FOR_ENTRY)
                logger.info(
                    "publication_path signal_id=%s stage=AI_REVIEW_PASSED verdict=%s",
                    signal.id,
                    review.verdict.value,
                )
            should_alert = signal.state is SignalState.ENTRY_READY or self.settings.send_watch_alerts
            signal = replace(
                signal,
                publication_state=(
                    PublicationState.PUBLISH_PENDING if should_alert else PublicationState.INTERNAL_ONLY
                ),
                intended_destination_ids=self.settings.telegram_delivery_ids,
            )
            logger.info(
                "publication_path signal_id=%s stage=SETUP_APPROVED symbol=%s strategy=%s setup_score=%d entry_score=%d state=%s publishable=%s",
                signal.id,
                symbol,
                candidate.strategy,
                score.total,
                entry_quality.total,
                signal.state.value,
                should_alert,
            )
            valid_signals.append(signal)
        selected = select_best_signal(valid_signals)
        if valid_signals and selected is None:
            logger.info("setup_rejected symbol=%s reason=AMBIGUOUS_DIRECTIONS candidates=%d", symbol, len(valid_signals))
        elif selected is not None:
            logger.info(
                "signal_selected symbol=%s strategy=%s score=%d suppressed_candidates=%d",
                symbol,
                selected.strategy,
                selected.score,
                len(valid_signals) - 1,
            )
            persistence_enabled = self.outcomes is not None and (
                not self.settings.dry_run or self.settings.dry_run_track_outcomes
            )
            duplicate = self.store.find_duplicate(
                selected,
                window_minutes=self.settings.signal_dedup_window_minutes,
                entry_atr=self.settings.signal_dedup_entry_atr,
                stop_atr=self.settings.signal_dedup_stop_atr,
                target_atr=self.settings.signal_dedup_target_atr,
            )
            if duplicate is not None:
                self.health.duplicate_candidates_suppressed += 1
                logger.info(
                    "publication_path signal_id=%s stage=DEDUP_RESULT publish=false matched_signal_id=%s "
                    "symbol=%s setup_fingerprint=%s reason=%s",
                    selected.id,
                    duplicate.signal.id,
                    symbol,
                    selected.setup_fingerprint,
                    duplicate.reason,
                )
                existing = duplicate.signal
                if (
                    existing.publication_state is PublicationState.INTERNAL_ONLY
                    and selected.state is SignalState.ENTRY_READY
                    and selected.entry_quality is not None
                ):
                    existing_ready = self.store.mark_entry_ready(
                        existing.id,
                        selected.entry_quality,
                        observed_at=selected.created_at,
                        current_price=selected.current_price or selected.trade.preferred_entry,
                        minimum_score=(
                            self.settings.scalp_minimum_entry_score
                            if selected.mode is SignalMode.SCALP
                            else self.settings.minimum_entry_score
                        ),
                    )
                    if existing_ready is not None:
                        await self._publish_lifecycle_events([existing_ready], symbol)
                elif existing.publication_state in {
                    PublicationState.PUBLISH_PENDING,
                    PublicationState.PUBLISH_FAILED,
                }:
                    await self._ensure_initial_publication(existing)
            else:
                logger.info(
                    "publication_path signal_id=%s stage=DEDUP_RESULT publish=true matched_signal_id=none",
                    selected.id,
                )
                parent = self.store.find_reentry_parent(selected)
                if parent is not None:
                    selected = replace(
                        selected,
                        signal_type="RE_ENTRY",
                        parent_signal_id=parent.id,
                    )
                    self.health.reentries_issued += 1
                if self.store.concurrent_open_count(symbol):
                    self.health.same_symbol_concurrent_signals += 1
                self.store.restore(selected)
                if persistence_enabled and self.outcomes is not None:
                    inserted = await self.outcomes.record_signal(selected)
                    if not inserted:
                        self.store.discard(selected.id)
                        logger.info("signal_id_collision_suppressed symbol=%s signal_id=%s", symbol, selected.id)
                        return SymbolScanResult(True)
                    logger.info(
                        "publication_path signal_id=%s stage=SIGNAL_PERSISTED backend=%s",
                        selected.id,
                        self.outcomes.backend,
                    )
                self.health.signals_issued += 1
                logger.info(
                    "signal_instance_issued signal_id=%s setup_fingerprint=%s type=%s parent_signal_id=%s",
                    selected.id,
                    selected.setup_fingerprint,
                    selected.signal_type,
                    selected.parent_signal_id or "none",
                )
                chart_png: bytes | None = None
                if selected.publication_state is PublicationState.PUBLISH_PENDING:
                    try:
                        chart_png = render_signal_chart(
                            selected,
                            series["1h"],
                            context.timeframes["1h"].indicators,
                            context.timeframes["1h"].zones,
                        )
                    except Exception as exc:
                        logger.warning(
                            "chart_render_failure signal_id=%s symbol=%s error=%s message=%s",
                            selected.id,
                            symbol,
                            type(exc).__name__,
                            str(exc)[:300],
                        )
                    await self._ensure_initial_publication(selected, chart_png=chart_png)
                else:
                    logger.info(
                        "publication_path signal_id=%s stage=INITIAL_PUBLICATION_DEFERRED reason=watch_alerts_disabled entry_quality=%d",
                        selected.id,
                        selected.entry_quality.total if selected.entry_quality else 0,
                    )
                monitored = self.store.get(selected.id) or selected
                if selected.state is SignalState.CREATED:
                    monitored = transition(
                        selected,
                        SignalState.WAITING_ENTRY,
                        current_price=selected.current_price,
                        changed_at=selected.created_at,
                    )
                    self.store.restore(monitored)
                    if persistence_enabled and self.outcomes is not None:
                        waiting_recorded = await self.outcomes.record_event(monitored)
                        if not waiting_recorded:
                            logger.warning(
                                "setup_waiting_persistence_failure symbol=%s signal_id=%s",
                                symbol,
                                selected.id,
                            )
                # If publication occurs while price is already inside the zone,
                # emit exactly one activation event immediately after the setup.
                await self._publish_lifecycle_events(
                    self.store.track_price(
                        symbol,
                        monitored.current_price or monitored.trade.preferred_entry,
                        observed_at=monitored.created_at + timedelta(microseconds=1),
                    ),
                    symbol,
                )
        for timeframe in ("15m", "5m"):
            if timeframe not in series:
                continue
            closed = series[timeframe]
            await self._publish_lifecycle_events(
                self.store.track_candles(
                    symbol,
                    closed.timestamp,
                    closed.high,
                    closed.low,
                    closed.close,
                    timeframe_ms=900_000 if timeframe == "15m" else 300_000,
                    trading_timeframe=timeframe,
                ),
                symbol,
            )
        return SymbolScanResult(True)
