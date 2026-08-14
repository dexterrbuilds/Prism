from __future__ import annotations

from app.models import Direction, Signal


def _price(value: float) -> str:
    if value >= 1_000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.4f}"
    return f"${value:,.6f}"


def format_signal(signal: Signal) -> str:
    icon = "🟢" if signal.direction is Direction.LONG else "🔴"
    evidence = "\n".join(f"• {item}" for item in signal.evidence[:8])
    strategy = signal.strategy.replace("_", " ").title()
    return (
        f"{icon} *{signal.symbol} — {signal.direction.value}*\n\n"
        f"*Strategy*\n{strategy}\n"
        f"*Market Regime*\n{signal.regime.value.replace('_', ' ').title()}\n"
        f"*Confluence Score*\n{signal.score}/100\n"
        f"*Entry Zone*\n{_price(signal.trade.entry_zone_low)} – {_price(signal.trade.entry_zone_high)}\n"
        f"*Preferred Entry*\n{_price(signal.trade.preferred_entry)}\n"
        f"*Stop Loss*\n{_price(signal.trade.stop_loss)}\n"
        f"*TP1*\n{_price(signal.trade.tp1)}\n"
        f"*TP2 — 2R*\n{_price(signal.trade.tp2)}\n"
        f"*Risk : Reward*\n1 : {signal.trade.reward_risk:.2f}\n"
        f"*Evidence*\n{evidence}\n"
        f"*Invalidation*\n{signal.trade.invalidation_reason}\n\n"
        "⚠️ Technical-analysis research signal. Not financial advice."
    )


def format_watch(signal: Signal) -> str:
    return (
        f"🟡 *{signal.symbol} — WATCH*\n\n"
        f"*Setup*\n{signal.strategy.replace('_', ' ').title()}\n"
        f"*Confluence Score*\n{signal.score}/100\n"
        f"*Confirmation Required*\n{signal.trade.trigger}\n"
        f"*Invalidation*\n{_price(signal.trade.stop_loss)}"
    )


def format_lifecycle(signal: Signal) -> str:
    return f"ℹ️ *{signal.symbol} {signal.direction.value}* — {signal.state.value.replace('_', ' ')} at {_price(signal.trade.preferred_entry)}"
