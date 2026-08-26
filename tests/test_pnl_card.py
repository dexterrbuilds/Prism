from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from PIL import Image

from app.models import Direction, SignalState, TradePlan
from app.telegram.pnl_card import HEIGHT, WIDTH, pnl_card_data_from_signal, render_pnl_card
from app.telegram.pnl_cards import MascotState, MascotThresholds, PnlCardData, generate_pnl_card
from app.telegram.pnl_cards.content import select_context_message, select_mascot, select_quote
from app.telegram.pnl_cards.formatters import format_signed_percent, format_signed_usd
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
    assert len(payload) < 5_000_000


def test_pnl_card_rejects_non_target_state() -> None:
    with pytest.raises(ValueError):
        render_pnl_card(make_signal(state=SignalState.ACTIVE))


def test_tp2_pnl_card_renders_runner_completion() -> None:
    started = datetime.now(UTC) - timedelta(hours=5)
    signal = replace(
        make_signal(state=SignalState.TP2_HIT),
        current_price=110,
        activated_at=started,
        tp1_hit_at=started + timedelta(hours=2),
        state_changed_at=started + timedelta(hours=5),
    )

    payload = render_pnl_card(signal)

    assert payload.startswith(b"\x89PNG")
    assert len(payload) < 5_000_000


def test_formatters_preserve_sign_and_group_large_values() -> None:
    assert format_signed_usd(1_234_567.89) == "+$1,234,567.89"
    assert format_signed_usd(-432.18) == "−$432.18"
    assert format_signed_percent(32.14) == "+32.14%"
    assert format_signed_percent(-4.32) == "−4.32%"


def test_mascot_selection_thresholds_and_override_are_explicit() -> None:
    base = PnlCardData("SOL/USDT", Direction.LONG, 100, 10)
    thresholds = MascotThresholds(huge_win_percent=50, big_win_percent=20, large_loss_percent=-15)
    assert select_mascot(replace(base, pnl_percent=55), thresholds) is MascotState.HUGE_WIN
    assert select_mascot(replace(base, pnl_percent=25), thresholds) is MascotState.BIG_WIN
    assert select_mascot(replace(base, pnl_usd=-100, pnl_percent=-20), thresholds) is MascotState.LOSS
    assert select_mascot(replace(base, pnl_usd=-20, pnl_percent=-2), thresholds) is MascotState.SMALL_LOSS
    assert select_mascot(replace(base, mascot_state=MascotState.STREAK), thresholds) is MascotState.STREAK


def test_content_rotation_is_stable_and_manual_values_win() -> None:
    data = PnlCardData("ETH/USDT", Direction.SHORT, 250, 5, content_seed="trade-42")
    assert select_quote(data) == select_quote(data)
    assert select_context_message(data) == select_context_message(data)
    overridden = replace(data, quote="Follow the plan.", context_message="Locked in.")
    assert select_quote(overridden) == "Follow the plan."
    assert select_context_message(overridden) == "Locked in."


def test_username_is_sanitized_without_at_prefix() -> None:
    data = PnlCardData("SOL/USDT", Direction.LONG, 100, 5, username="@prismquantbot")
    assert data.username == "prismquantbot"


def test_profitable_short_keeps_short_direction() -> None:
    short_trade = TradePlan(99, 101, 100, "retest", "hold", 105, 5, 2.5, "above structure", 95, 90, None, 2)
    short = replace(make_signal(state=SignalState.TP1_HIT), direction=Direction.SHORT, trade=short_trade, current_price=95)
    data = pnl_card_data_from_signal(short)
    assert data.direction is Direction.SHORT
    assert data.pnl_usd > 0
    assert data.pnl_percent > 0


def test_pre_tp1_stop_generates_a_premium_loss_card() -> None:
    stopped = replace(make_signal(state=SignalState.STOPPED), current_price=95, tp1_hit_at=None)
    data = pnl_card_data_from_signal(stopped)
    assert data.direction is Direction.LONG
    assert data.pnl_usd < 0
    payload = render_pnl_card(stopped)
    with Image.open(BytesIO(payload)) as image:
        assert image.size == (1200, 1200)


def test_new_sl_hit_state_generates_a_premium_loss_card() -> None:
    stopped = replace(make_signal(state=SignalState.SL_HIT), current_price=95, tp1_hit_at=None)
    data = pnl_card_data_from_signal(stopped)
    assert data.pnl_usd < 0
    with Image.open(BytesIO(render_pnl_card(stopped))) as image:
        assert image.size == (1200, 1200)


def test_generic_generator_handles_large_values_custom_chart_and_pair() -> None:
    payload = generate_pnl_card(
        PnlCardData(
            "1000PEPE/USDT",
            Direction.SHORT,
            1_234_567.89,
            51.25,
            chart_data=(10, 9, 12, 11, 16, 18),
            leverage=5,
            mascot_state=MascotState.STREAK,
        )
    )
    assert payload.startswith(b"\x89PNG")
    with Image.open(BytesIO(payload)) as image:
        assert image.size == (WIDTH, HEIGHT)
