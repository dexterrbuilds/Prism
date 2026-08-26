from __future__ import annotations

from app.models import Direction, Signal, SignalState
from app.telegram.pnl_cards import HEIGHT, WIDTH, PnlCardData, generate_pnl_card

DEFAULT_MARGIN_USD = 5_000.0
DEFAULT_LEVERAGE = 5.0


def _duration(signal: Signal) -> str | None:
    if signal.activated_at is None or signal.state_changed_at is None:
        return None
    hours = max(0.0, (signal.state_changed_at - signal.activated_at).total_seconds() / 3600)
    return f"Held {max(1, round(hours * 60))}m" if hours < 1 else f"Held {hours:.1f}h"


def pnl_card_data_from_signal(
    signal: Signal,
    *,
    margin_usd: float = DEFAULT_MARGIN_USD,
    leverage: float = DEFAULT_LEVERAGE,
    username: str = "prismquantbot",
) -> PnlCardData:
    """Adapt a lifecycle event to an explicitly labeled leverage simulation."""
    if margin_usd <= 0 or leverage <= 0:
        raise ValueError("margin_usd and leverage must be positive")
    if signal.state is SignalState.TP2_HIT:
        exit_price = signal.trade.tp2
    elif signal.state is SignalState.TP1_HIT:
        exit_price = signal.trade.tp1
    elif signal.state is SignalState.STOPPED and signal.tp1_hit_at is None:
        exit_price = signal.trade.stop_loss
    else:
        raise ValueError("PnL cards require TP1, TP2, or a pre-TP1 stop event")

    entry = signal.trade.preferred_entry
    move_per_unit = exit_price - entry if signal.direction is Direction.LONG else entry - exit_price
    quantity = margin_usd * leverage / entry
    pnl_usd = move_per_unit * quantity
    pnl_percent = pnl_usd / margin_usd * 100
    return PnlCardData(
        pair=signal.symbol,
        direction=signal.direction,
        pnl_usd=pnl_usd,
        pnl_percent=pnl_percent,
        entry_price=entry,
        exit_price=exit_price,
        mark_price=signal.current_price,
        leverage=leverage,
        realized_pnl=pnl_usd,
        trade_duration=_duration(signal),
        calculation_label="$5K MARGIN SIMULATION",
        username=username,
        content_seed=f"{signal.id}|{signal.state.value}",
    )


def render_pnl_card(signal: Signal) -> bytes:
    return generate_pnl_card(pnl_card_data_from_signal(signal))


__all__ = (
    "DEFAULT_LEVERAGE",
    "DEFAULT_MARGIN_USD",
    "HEIGHT",
    "WIDTH",
    "pnl_card_data_from_signal",
    "render_pnl_card",
)
