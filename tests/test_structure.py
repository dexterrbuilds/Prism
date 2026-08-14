from __future__ import annotations

import numpy as np

from app.analysis.structure import detect_structure, detect_swings
from app.models import Direction, SwingKind, SwingLabel
from tests.helpers import candles


def test_swing_detection_labels_and_confirmation_are_lookahead_safe() -> None:
    close = np.full(18, 100.0)
    high = np.full(18, 101.0)
    low = np.full(18, 99.0)
    high[[3, 9]] = [105, 108]
    low[[6, 12]] = [95, 97]
    series = candles(close, high=high, low=low)
    swings = detect_swings(series, left=2, right=2)
    highs = [s for s in swings if s.kind is SwingKind.HIGH]
    lows = [s for s in swings if s.kind is SwingKind.LOW]
    assert [s.label for s in highs] == [SwingLabel.HIGH, SwingLabel.HH]
    assert [s.label for s in lows] == [SwingLabel.LOW, SwingLabel.HL]
    assert all(s.confirmed_at_index == s.index + 2 for s in swings)


def test_bos_detected_only_on_closed_cross() -> None:
    close = np.full(20, 100.0)
    high = np.full(20, 101.0)
    low = np.full(20, 99.0)
    high[6] = 106
    low[10] = 95
    close[-2:] = [105, 107]
    high[-2:] = [105.5, 108]
    low[-2:] = [104, 105]
    state = detect_structure(candles(close, high=high, low=low), left=2, right=2)
    assert any(event.name == "BOS" and event.direction is Direction.LONG and event.level == 106 for event in state.events)


def test_choch_when_cross_opposes_established_bias() -> None:
    # Alternating lower highs/lows creates bearish bias before the final bullish crossing.
    close = np.full(40, 90.0)
    # Geometry is intentionally sparse here so the requested pivots are unambiguous.
    high = np.full(40, 80.0)
    low = np.full(40, 120.0)
    for index, value in ((5, 110), (13, 105), (21, 100), (29, 98)):
        high[index] = value
    for index, value in ((9, 100), (17, 95), (25, 90), (33, 85)):
        low[index] = value
    close[-2:] = [97, 101]
    high[-2:] = [97.5, 102]
    low[-2:] = [96, 97]
    state = detect_structure(candles(close, high=high, low=low), left=2, right=2)
    assert any(event.name == "CHOCH" and event.direction is Direction.LONG for event in state.events)
