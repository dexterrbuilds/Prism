from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

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

    async def fetch_prices(self, symbols: tuple[str, ...]) -> dict[str, float]:
        return {symbol: 100.0 for symbol in symbols}


class FakeTelegram:
    def __init__(self) -> None:
        self.published = 0
        self.signals: list[Signal] = []

    async def publish(self, signal, lifecycle: bool = False, chart_png: bytes | None = None) -> bool:
        del lifecycle, chart_png
        self.published += 1
        self.signals.append(signal)
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


@pytest.mark.asyncio
async def test_lifecycle_monitor_emits_entry_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    telegram = FakeTelegram()
    scanner = Scanner(Settings.from_env(), FakeExchange(), telegram, RuntimeHealth("fake"))  # type: ignore[arg-type]
    async def no_reconciliation(*, startup: bool = False) -> int:
        del startup
        return 0
    scanner.reconcile_open_signals = no_reconciliation  # type: ignore[method-assign]
    from tests.test_lifecycle import make_waiting_signal

    now = datetime.now(UTC)
    scanner.store.restore(
        replace(
            make_waiting_signal(),
            created_at=now - timedelta(minutes=1),
            state_changed_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=6),
        )
    )
    await scanner._monitor_open_setups()
    await scanner._monitor_open_setups()

    entry_events = [signal for signal in telegram.signals if signal.state is SignalState.ENTRY_TRIGGERED]
    assert len(entry_events) == 1


def _ranked_signal(
    strategy: str,
    direction: Direction,
    score: int,
    entry: float = 100,
    *,
    symbol: str = "BTC/USDT",
) -> Signal:
    trade = TradePlan(99, 101, entry, "retest", "hold", 95, 5, 2.5, "below structure", 105, 110, None, 2, 8, 20)
    return Signal(
        strategy, symbol, strategy, direction, MarketRegime.BULLISH_TREND,
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


def test_each_pair_selects_its_own_best_qualifying_setup() -> None:
    qualifying = [
        _ranked_signal("BREAKOUT", Direction.LONG, 82, symbol="BTC/USDT"),
        _ranked_signal("BREAKOUT_RETEST", Direction.LONG, 91, symbol="BTC/USDT"),
        _ranked_signal("EMA_PULLBACK", Direction.LONG, 84, symbol="ETH/USDT"),
        _ranked_signal("BOS_CONTINUATION", Direction.LONG, 88, symbol="ETH/USDT"),
        _ranked_signal("LIQUIDITY_SWEEP_REVERSAL", Direction.LONG, 86, symbol="SOL/USDT"),
    ]
    selected = {
        symbol: select_best_signal([signal for signal in qualifying if signal.symbol == symbol])
        for symbol in {signal.symbol for signal in qualifying}
    }

    assert set(selected) == {"BTC/USDT", "ETH/USDT", "SOL/USDT"}
    assert selected["BTC/USDT"] is not None
    assert selected["BTC/USDT"].strategy == "BREAKOUT_RETEST"
    assert selected["ETH/USDT"] is not None
    assert selected["ETH/USDT"].strategy == "BOS_CONTINUATION"
    assert selected["SOL/USDT"] is not None
    assert selected["SOL/USDT"].strategy == "LIQUIDITY_SWEEP_REVERSAL"


def test_near_tied_opposite_directions_are_rejected() -> None:
    assert select_best_signal(
        [
            _ranked_signal("BREAKOUT", Direction.LONG, 86),
            _ranked_signal("FAILED_BREAKOUT", Direction.SHORT, 83),
        ]
    ) is None


@pytest.mark.asyncio
async def test_pnl_card_failure_cannot_fail_symbol_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr("app.scanner.detect_setups", lambda context: [])

    def fail_card(signal: Signal) -> bytes:
        del signal
        raise RuntimeError("render failure")

    monkeypatch.setattr("app.scanner.render_pnl_card", fail_card)
    telegram = FakeTelegram()
    scanner = Scanner(Settings.from_env(), FakeExchange(), telegram, RuntimeHealth("fake"))  # type: ignore[arg-type]
    as_of_ms = 1_800_000_000_000
    active = _ranked_signal("BREAKOUT_RETEST", Direction.LONG, 88)
    active = Signal(
        id=active.id,
        symbol=active.symbol,
        strategy=active.strategy,
        direction=active.direction,
        regime=active.regime,
        score=active.score,
        grade=active.grade,
        state=active.state,
        trade=active.trade,
        evidence=active.evidence,
        created_at=datetime.fromtimestamp((as_of_ms - 1_800_000) / 1000, UTC),
        current_price=100,
        state_changed_at=datetime.fromtimestamp((as_of_ms - 1_800_000) / 1000, UTC),
        activated_at=datetime.fromtimestamp((as_of_ms - 1_800_000) / 1000, UTC),
    )
    scanner.store.restore(active)

    result = await scanner._scan_symbol("BTC/USDT", as_of_ms)

    assert result.success
    assert any(signal.state is SignalState.TP1_HIT for signal in telegram.signals)
