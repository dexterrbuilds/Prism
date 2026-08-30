from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from app.ai.analyst import AIReviewService
from app.analysis.bias import derive_directional_bias
from app.analysis.context import analyze_snapshot
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
    RejectionReason,
    SetupCandidate,
    Signal,
    SignalMode,
    SignalState,
)
from app.signals.entry_plan import calculate_entry_plan
from app.signals.entry_quality import score_entry_quality, two_gate_result
from app.signals.lifecycle import SignalStore, transition
from app.signals.repository import OutcomeRepository
from app.signals.risk import RiskPlanningError, build_trade_plan
from app.signals.scoring import score_candidate
from app.signals.validator import validate_candidate
from app.signals.validity import derive_setup_validity_minutes
from app.strategies import detect_setups
from app.strategies.counter import evaluate_counter_setup
from app.telegram.bot import TelegramService
from app.telegram.chart import render_signal_chart
from app.telegram.pnl_card import render_pnl_card

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SymbolScanResult:
    success: bool
    error: str | None = None


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

    async def restore_outcomes(self) -> None:
        """Restore persisted open theses before the first market scan."""
        if self.outcomes is None:
            return
        restored = await self.outcomes.load_open_signals()
        for signal in restored:
            self.store.restore(signal)
        logger.info("signal_outcomes_restored count=%d", len(restored))

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
                    signal.symbol,
                    signal.direction,
                    quality,
                    observed_at=observed_at,
                    current_price=series[execution_tf].latest_close,
                )
                if ready is not None:
                    await self._publish_lifecycle_events([ready], signal.symbol)
            except (ValueError, ArithmeticError) as exc:
                logger.warning("entry_refresh_failure symbol=%s error=%s", signal.symbol, type(exc).__name__)

    async def _publish_lifecycle_events(self, events: list[Signal], symbol: str) -> None:
        for event in events:
            logger.info(
                "signal_lifecycle symbol=%s strategy=%s state=%s",
                event.symbol,
                event.strategy,
                event.state.value,
            )
            publish_event = True
            if self.outcomes is not None and (not self.settings.dry_run or self.settings.dry_run_track_outcomes):
                publish_event = await self.outcomes.record_event(event)
            if not publish_event:
                logger.info(
                    "signal_lifecycle_duplicate_suppressed symbol=%s state=%s",
                    event.symbol,
                    event.state.value,
                )
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
            await self.telegram.publish(event, lifecycle=True, chart_png=lifecycle_card)

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
        if not candidates:
            logger.info("setup_rejected symbol=%s reason=%s", symbol, RejectionReason.NO_SETUP.value)
        valid_signals: list[Signal] = []
        for candidate in candidates:
            score = score_candidate(candidate, context)
            setup_minimum = self.settings.scalp_minimum_setup_score if candidate.mode is SignalMode.SCALP else self.settings.minimum_valid_score
            if score.total < setup_minimum:
                logger.info("setup_rejected symbol=%s strategy=%s score=%d reason=%s", symbol, candidate.strategy, score.total, RejectionReason.LOW_CONFLUENCE.value)
                continue
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
                logger.info("setup_rejected symbol=%s strategy=%s reason=%s", symbol, candidate.strategy, RejectionReason.POOR_RR.value)
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
                logger.info("setup_rejected symbol=%s strategy=%s score=%d reason=%s", symbol, candidate.strategy, score.total, validation.reason.value if validation.reason else "UNKNOWN")
                continue
            gate = two_gate_result(score.total, entry_quality, setup_minimum, entry_minimum)
            if gate.hard_reject:
                logger.info("setup_rejected symbol=%s strategy=%s reason=%s", symbol, candidate.strategy, entry_quality.hard_reasons[0])
                continue
            ready = gate.actionable and entry_quality.retest_completed and entry_quality.lower_timeframe_confirmed
            state = SignalState.ENTRY_READY if ready else SignalState.WAITING_FOR_ENTRY
            bucket_ms = 300_000 if candidate.mode is SignalMode.SCALP else 3_600_000
            raw_id = f"{symbol}|{candidate.mode.value}|{candidate.strategy}|{candidate.direction.value}|{candidate.detected_at_ms // bucket_ms}"
            created_at = datetime.fromtimestamp(as_of_ms / 1000, UTC)
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
            valid_conditions = (
                f"Price remains {relation} {invalidation_level:.8g}",
                f"Price does not close {missed_relation} {missed_limit:.8g} before entry",
                f"Entry triggers before {expires_at.strftime('%H:%M UTC')}",
            )
            signal = Signal(
                id=sha256(raw_id.encode()).hexdigest()[:20], symbol=symbol, strategy=candidate.strategy,
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
            )
            if self.ai_reviews is not None and self.ai_reviews.enabled:
                review = await self.ai_reviews.review(signal)
                signal = replace(signal, ai_review=review)
                if review.verdict is AIReviewVerdict.REJECT:
                    logger.info("setup_rejected symbol=%s strategy=%s reason=AI_REJECT", symbol, candidate.strategy)
                    continue
                if review.verdict is AIReviewVerdict.WAIT:
                    signal = replace(signal, state=SignalState.WAITING_FOR_ENTRY)
            logger.info("signal_confirmed symbol=%s strategy=%s score=%d state=%s", symbol, candidate.strategy, score.total, state.value)
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
            persisted_duplicate = (
                persistence_enabled
                and self.outcomes is not None
                and await self.outcomes.contains_signal(selected.id)
            )
            if persisted_duplicate:
                logger.info("signal_duplicate_suppressed symbol=%s signal_id=%s", symbol, selected.id)
            elif self.store.should_publish(selected):
                if persistence_enabled and self.outcomes is not None:
                    inserted = await self.outcomes.record_signal(selected)
                    if not inserted:
                        logger.info("signal_duplicate_suppressed symbol=%s signal_id=%s", symbol, selected.id)
                        return SymbolScanResult(True)
                chart_png: bytes | None = None
                try:
                    chart_png = render_signal_chart(
                        selected,
                        series["1h"],
                        context.timeframes["1h"].indicators,
                        context.timeframes["1h"].zones,
                    )
                except Exception as exc:
                    logger.warning("chart_render_failure symbol=%s error=%s", symbol, type(exc).__name__)
                should_alert = selected.state is SignalState.ENTRY_READY or self.settings.send_watch_alerts
                if should_alert:
                    await self.telegram.publish(selected, chart_png=chart_png)
                else:
                    logger.info("watch_suppressed symbol=%s strategy=%s entry_quality=%d", symbol, selected.strategy, selected.entry_quality.total if selected.entry_quality else 0)
                monitored = selected
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
