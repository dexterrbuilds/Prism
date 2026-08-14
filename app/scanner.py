from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from hashlib import sha256

from app.analysis.context import analyze_snapshot
from app.analysis.data_quality import validate_candles
from app.api.health import RuntimeHealth
from app.config import Settings
from app.exchange.client import ExchangeClient
from app.models import MarketSnapshot, RejectionReason, Signal, SignalGrade, SignalState
from app.signals.lifecycle import SignalStore
from app.signals.risk import RiskPlanningError, build_trade_plan
from app.signals.scoring import score_candidate
from app.signals.validator import validate_candidate
from app.strategies import detect_setups
from app.telegram.bot import TelegramService

logger = logging.getLogger(__name__)


class Scanner:
    def __init__(self, settings: Settings, exchange: ExchangeClient, telegram: TelegramService, health: RuntimeHealth) -> None:
        self.settings = settings
        self.exchange = exchange
        self.telegram = telegram
        self.health = health
        self.store = SignalStore(max_size=128)

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
            for symbol, result in zip(self.settings.watchlist, results, strict=True):
                if isinstance(result, BaseException) or result is False:
                    failures += 1
                    if isinstance(result, BaseException):
                        logger.error("symbol_scan_failure symbol=%s error=%s", symbol, type(result).__name__)
            self.exchange.clear_cycle_cache()
            self.health.last_scan_ms = as_of_ms
            self.health.scanned_symbols = len(self.settings.watchlist) - failures
            self.health.scan_errors += failures
            self.health.scanner = "sleeping"
            logger.info("scan_completed duration_seconds=%.3f failures=%d", time.monotonic() - started, failures)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self.settings.scan_interval_seconds)
        self.health.scanner = "stopped"

    async def _scan_symbol(self, symbol: str, as_of_ms: int) -> bool:
        fetched = await asyncio.gather(
            *(self.exchange.fetch_ohlcv(symbol, timeframe, as_of_ms) for timeframe in self.settings.timeframes),
            return_exceptions=True,
        )
        series = {}
        for timeframe, result in zip(self.settings.timeframes, fetched, strict=True):
            if isinstance(result, BaseException):
                logger.warning("request_failure symbol=%s timeframe=%s error=%s", symbol, timeframe, type(result).__name__)
                logger.info("setup_rejected symbol=%s reason=%s", symbol, RejectionReason.DATA_QUALITY.value)
                return False
            quality = validate_candles(result)
            if not quality.valid:
                logger.info("setup_rejected symbol=%s timeframe=%s reason=%s details=%s", symbol, timeframe, RejectionReason.DATA_QUALITY.value, ",".join(quality.reasons))
                return False
            series[timeframe] = result
        snapshot = MarketSnapshot(symbol, series, as_of_ms)
        try:
            context = analyze_snapshot(snapshot, self.settings.pivot_left, self.settings.pivot_right, self.settings.zone_atr_tolerance)
        except (ValueError, ArithmeticError) as exc:
            logger.warning("indicator_failure symbol=%s error=%s", symbol, type(exc).__name__)
            return False
        candidates = detect_setups(context)
        if not candidates:
            logger.info("setup_rejected symbol=%s reason=%s", symbol, RejectionReason.NO_SETUP.value)
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
            in_zone = plan.entry_zone_low <= series["1h"].latest_close <= plan.entry_zone_high
            state = SignalState.ACTIVE if candidate.confirmed and in_zone else SignalState.CONFIRMED if candidate.confirmed else SignalState.WATCHING
            raw_id = f"{symbol}|{candidate.strategy}|{candidate.direction.value}|{candidate.detected_at_ms // 3_600_000}"
            signal = Signal(
                id=sha256(raw_id.encode()).hexdigest()[:20], symbol=symbol, strategy=candidate.strategy,
                direction=candidate.direction, regime=context.regime, score=score.total, grade=score.grade,
                state=state, trade=plan, evidence=score.evidence, created_at=datetime.fromtimestamp(as_of_ms / 1000, UTC),
            )
            logger.info("signal_confirmed symbol=%s strategy=%s score=%d state=%s", symbol, candidate.strategy, score.total, state.value)
            if self.store.should_publish(signal):
                await self.telegram.publish(signal)
        for event in self.store.track_price(symbol, series["15m"].latest_close):
            logger.info("signal_lifecycle symbol=%s strategy=%s state=%s", event.symbol, event.strategy, event.state.value)
            await self.telegram.publish(event, lifecycle=True)
        return True
