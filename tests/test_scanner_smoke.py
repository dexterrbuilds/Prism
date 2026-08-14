from __future__ import annotations

import numpy as np
import pytest

from app.api.health import RuntimeHealth
from app.config import Settings
from app.models import CandleSeries
from app.scanner import Scanner


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

    async def publish(self, signal, lifecycle: bool = False) -> bool:
        self.published += 1
        return True


@pytest.mark.asyncio
async def test_complete_offline_dry_run_scan_does_not_require_a_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    settings = Settings.from_env()
    telegram = FakeTelegram()
    scanner = Scanner(settings, FakeExchange(), telegram, RuntimeHealth("fake"))  # type: ignore[arg-type]
    assert await scanner._scan_symbol("BTC/USDT", 1_800_000_000_000)
