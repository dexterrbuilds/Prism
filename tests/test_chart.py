from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

import numpy as np
from PIL import Image

from app.models import Direction, MarketRegime, Signal, SignalGrade, SignalState, TradePlan
from app.telegram.chart import HEIGHT, WIDTH, render_signal_chart
from tests.helpers import candles, indicators


def test_signal_chart_is_a_bounded_png_with_analysis_overlays() -> None:
    close = 100 + np.arange(250, dtype=np.float64) * 0.05 + np.sin(np.arange(250) / 5)
    series = candles(close)
    trade = TradePlan(110, 111, 110.5, "retest", "hold", 107, 3.5, 1.75, "below structure", 114, 117.5, None, 2, 8, 24)
    signal = Signal(
        "id", "BTC/USDT", "BREAKOUT_RETEST", Direction.LONG, MarketRegime.BULLISH_TREND,
        88, SignalGrade.VALID, SignalState.ACTIVE, trade, ("evidence",), datetime.now(UTC),
    )
    payload = render_signal_chart(signal, series, indicators(250, price=series.latest_close), ())
    assert payload.startswith(b"\x89PNG")
    with Image.open(BytesIO(payload)) as image:
        assert image.size == (WIDTH, HEIGHT)
    assert len(payload) < 1_000_000
