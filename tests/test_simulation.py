from __future__ import annotations

import pytest

from app.signals.simulation import simulate_leverage
from tests.test_lifecycle import make_signal


def test_5000_margin_simulation_scales_linearly_with_leverage() -> None:
    signal = make_signal(entry=100)
    two_x = simulate_leverage(signal, 5_000, 2)
    five_x = simulate_leverage(signal, 5_000, 5)
    assert two_x.notional_usd == 10_000
    assert two_x.quantity == 100
    assert two_x.stop_pnl_usd == pytest.approx(-500)
    assert two_x.tp1_pnl_usd == pytest.approx(500)
    assert two_x.tp2_pnl_usd == pytest.approx(1_000)
    assert five_x.tp2_pnl_usd == pytest.approx(two_x.tp2_pnl_usd * 2.5)
