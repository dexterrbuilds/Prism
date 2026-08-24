from __future__ import annotations

from dataclasses import dataclass

from app.models import Direction, Signal


@dataclass(frozen=True, slots=True)
class LeverageSimulation:
    margin_usd: float
    leverage: int
    notional_usd: float
    quantity: float
    stop_pnl_usd: float
    tp1_pnl_usd: float
    tp2_pnl_usd: float


def simulate_leverage(signal: Signal, margin_usd: float, leverage: int) -> LeverageSimulation:
    if margin_usd <= 0 or leverage <= 0 or signal.trade.preferred_entry <= 0:
        raise ValueError("simulation inputs must be positive")
    notional = margin_usd * leverage
    quantity = notional / signal.trade.preferred_entry
    sign = 1.0 if signal.direction is Direction.LONG else -1.0

    def pnl(exit_price: float) -> float:
        return (exit_price - signal.trade.preferred_entry) * quantity * sign

    return LeverageSimulation(
        margin_usd=margin_usd,
        leverage=leverage,
        notional_usd=notional,
        quantity=quantity,
        stop_pnl_usd=pnl(signal.trade.stop_loss),
        tp1_pnl_usd=pnl(signal.trade.tp1),
        tp2_pnl_usd=pnl(signal.trade.tp2),
    )
