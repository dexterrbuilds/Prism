from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from PIL import Image

from app.models import SignalState
from app.telegram.pnl_card import HEIGHT, WIDTH, render_pnl_card
from tests.test_lifecycle import make_signal


def test_tp1_pnl_card_is_bounded_png() -> None:
    started = datetime.now(UTC) - timedelta(hours=3)
    signal = replace(
        make_signal(state=SignalState.TP1_HIT),
        current_price=105,
        activated_at=started,
        tp1_hit_at=started + timedelta(hours=3),
        state_changed_at=started + timedelta(hours=3),
    )
    payload = render_pnl_card(signal)
    assert payload.startswith(b"\x89PNG")
    with Image.open(BytesIO(payload)) as image:
        assert image.size == (WIDTH, HEIGHT)
    assert len(payload) < 1_000_000


def test_pnl_card_rejects_non_target_state() -> None:
    with pytest.raises(ValueError):
        render_pnl_card(make_signal(state=SignalState.ACTIVE))
