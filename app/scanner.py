from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from app.analysis.context import analyze_snapshot
from app.analysis.data_quality import validate_candles
from app.api.health import RuntimeHealth
from app.config import Settings
from app.exchange.client import ExchangeClient
from app.models import Direction, MarketSnapshot, RejectionReason, Signal, SignalGrade, SignalState
from app.signals.lifecycle import SignalStore, transition
from app.signals.repository import OutcomeRepository
from app.signals.risk import RiskPlanningError, build_trade_plan
from app.signals.scoring import score_candidate
from app.signals.validator import validate_candidate
from app.signals.validity import derive_setup_validity_minutes
from app.strategies import detect_setups
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
        SignalState.WAITING_ENTRY: 3,
        SignalState.CONFIRMED: 3,
        SignalState.CREATED: 2,
        SignalState.WATCHING: 1,
    }
    ordered = sorted(signals, key=lambda signal: (signal.score, state_priority.get(signal.state, 0)), reverse=True)
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
    ) -> None:
        self.settings = settings
        self.exchange = exchange
        self.telegram = telegram
        self.health = health
        self.outcomes = outcomes
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
        try:
            prices = await self.exchange.fetch_prices(symbols)
        except Exception as exc:
            logger.warning("lifecycle_price_failure error=%s", type(exc).__name__)
            return
        for symbol, price in prices.items():
            await self._publish_lifecycle_events(
                self.store.track_price(symbol, price, observed_at=observed_at),
                symbol,
            )

    async def _publish_lifecycle_events(self, events: list[Signal], symbol: str) -> None:
        for event in events:
            logger.info(
                "signal_lifecycle symbol=%s strategy=%s state=%s",
                event.symbol,
                event.strategy,
                event.state.value,
            )
            publish_event = True
            if not self.settings.dry_run and self.outcomes is not None:
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
        candidates = detect_setups(context)
        if not candidates:
            logger.info("setup_rejected symbol=%s reason=%s", symbol, RejectionReason.NO_SETUP.value)
        valid_signals: list[Signal] = []
        for candidate in candidates:
            score = score_candidate(candidate, context)
            try:
                plan = build_trade_plan(candidate, series["1h"].latest_close, float(context.timeframes["1h"].indicators.atr[-1]), context.timeframes["1h"].zones)
            except RiskPlanningError:
                logger.info("setup_rejected symbol=%s strategy=%s reason=%s", symbol, candidate.strategy, RejectionReason.POOR_RR.value)
                continue
            validation = validate_candidate(candidate, context, score, plan, self.settings.max_chase_atr, self.settings.minimum_valid_score)
            if not validation.valid:
                logger.info("setup_rejected symbol=%s strategy=%s score=%d reason=%s", symbol, candidate.strategy, score.total, validation.reason.value if validation.reason else "UNKNOWN")
                continue
            if score.grade is SignalGrade.WATCH and not self.settings.send_watch_alerts:
                logger.info("watch_suppressed symbol=%s strategy=%s score=%d", symbol, candidate.strategy, score.total)
                continue
            state = SignalState.CREATED if candidate.confirmed else SignalState.WATCHING
            raw_id = f"{symbol}|{candidate.strategy}|{candidate.direction.value}|{candidate.detected_at_ms // 3_600_000}"
            created_at = datetime.fromtimestamp(as_of_ms / 1000, UTC)
            validity_minutes = derive_setup_validity_minutes(candidate, plan)
            expires_at = created_at + timedelta(minutes=validity_minutes)
            atr = float(context.timeframes["1h"].indicators.atr[-1])
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
                current_price=series["15m"].latest_close,
                state_changed_at=created_at,
                trading_timeframe="15m",
                analysis_timeframe=candidate.timeframe,
                expires_at=expires_at,
                validity_minutes=validity_minutes,
                valid_conditions=valid_conditions,
                max_missed_distance=missed_distance,
            )
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
            persisted_duplicate = (
                not self.settings.dry_run
                and self.outcomes is not None
                and await self.outcomes.contains_signal(selected.id)
            )
            if persisted_duplicate:
                logger.info("signal_duplicate_suppressed symbol=%s signal_id=%s", symbol, selected.id)
            elif self.store.should_publish(selected):
                if not self.settings.dry_run and self.outcomes is not None:
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
                await self.telegram.publish(selected, chart_png=chart_png)
                monitored = selected
                if selected.state is SignalState.CREATED:
                    monitored = transition(
                        selected,
                        SignalState.WAITING_ENTRY,
                        current_price=selected.current_price,
                        changed_at=selected.created_at,
                    )
                    self.store.restore(monitored)
                    if not self.settings.dry_run and self.outcomes is not None:
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
        closed_15m = series["15m"]
        await self._publish_lifecycle_events(
            self.store.track_candles(
                symbol,
                closed_15m.timestamp,
                closed_15m.high,
                closed_15m.low,
                closed_15m.close,
            ),
            symbol,
        )
        return SymbolScanResult(True)
