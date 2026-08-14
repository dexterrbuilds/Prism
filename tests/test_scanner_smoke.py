from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from app.api.health import RuntimeHealth
from app.config import Settings
from app.models import CandleSeries, Direction, MarketRegime, Signal, SignalGrade, SignalState, TradePlan
from app.scanner import Scanner, select_best_signal


class FakeExchange:
    async def fetch_ohlcv(self, symbol: str, timeframe: str, as_of_ms: int) -> CandleSeries:
        interval = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[timeframe]
        timestamp = as_of_ms - np.arange(250, 0, -1, dtype=np.int64) * interval
        phase = np.arange(250, dtype=np.float64)
        close = 100 + phase * 0.03 + np.sin(phase / 5) * 1.5
        open_ = close - np.sin(phase / 3) * 0.2
        return CandleSeries(
            symbol, timeframe, timestamp, open_, np.maximum(open_, close) + 0.5,
            np.minimum(open_, close) - 0.5, close, 100 + np.cos(phase / 4) * 10, as_of_ms,
        )

    def clear_cycle_cache(self) -> None:
        pass


class FakeTelegram:
    def __init__(self) -> None:
        self.published = 0

    async def publish(self, signal, lifecycle: bool = False, chart_png: bytes | None = None) -> bool:
        self.published += 1
        return True


@pytest.mark.asyncio
async def test_complete_offline_dry_run_scan_does_not_require_a_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    settings = Settings.from_env()
    telegram = FakeTelegram()
    scanner = Scanner(settings, FakeExchange(), telegram, RuntimeHealth("fake"))  # type: ignore[arg-type]
    result = await scanner._scan_symbol("BTC/USDT", 1_800_000_000_000)
    assert result.success


def test_manual_scan_request_is_queued_once_and_never_overlaps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    health = RuntimeHealth("fake", scanner="sleeping")
    scanner = Scanner(Settings.from_env(), FakeExchange(), FakeTelegram(), health)  # type: ignore[arg-type]
    assert scanner.request_manual_scan()
    assert not scanner.request_manual_scan()
    health.scanner = "running"
    scanner._manual_scan_event.clear()
    assert not scanner.request_manual_scan()


def _ranked_signal(strategy: str, direction: Direction, score: int, entry: float = 100) -> Signal:
    trade = TradePlan(99, 101, entry, "retest", "hold", 95, 5, 2.5, "below structure", 105, 110, None, 2, 8, 20)
    return Signal(
        strategy, "BTC/USDT", strategy, direction, MarketRegime.BULLISH_TREND,
        score, SignalGrade.VALID, SignalState.ACTIVE, trade, ("evidence",),
        datetime.now(UTC),
    )


def test_overlapping_strategies_collapse_to_one_ranked_thesis() -> None:
    selected = select_best_signal(
        [
            _ranked_signal("BREAKOUT", Direction.LONG, 82),
            _ranked_signal("BREAKOUT_RETEST", Direction.LONG, 88),
            _ranked_signal("BOS_CONTINUATION", Direction.LONG, 85),
        ]
    )
    assert selected is not None
    assert selected.strategy == "BREAKOUT_RETEST"
    assert set(selected.supporting_strategies) == {"BOS_CONTINUATION", "BREAKOUT"}


def test_near_tied_opposite_directions_are_rejected() -> None:
    assert select_best_signal(
        [
            _ranked_signal("BREAKOUT", Direction.LONG, 86),
            _ranked_signal("FAILED_BREAKOUT", Direction.SHORT, 83),
        ]
    ) is None
