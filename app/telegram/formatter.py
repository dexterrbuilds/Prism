from __future__ import annotations

from datetime import UTC, datetime

from app.api.health import RuntimeHealth
from app.config import Settings
from app.models import Direction, Signal
from app.signals.simulation import simulate_leverage


def _price(value: float) -> str:
    if value >= 1_000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.4f}"
    return f"${value:,.6f}"


def _signed_money(value: float) -> str:
    sign = "+" if value >= 0 else "−"
    return f"{sign}${abs(value):,.0f}"


def format_signal(signal: Signal) -> str:
    icon = "🟢" if signal.direction is Direction.LONG else "🔴"
    evidence = "\n".join(f"• {item[:64]}" for item in signal.evidence[:4])
    strategy = signal.strategy.replace("_", " ").title()
    supporting = ""
    if signal.supporting_strategies:
        names = " · ".join(name.replace("_", " ").title() for name in signal.supporting_strategies[:2])
        supporting = f"\nSupports: {names}"
    hold_time = "Not calibrated"
    if signal.trade.estimated_hold_hours_low is not None and signal.trade.estimated_hold_hours_high is not None:
        hold_time = f"{signal.trade.estimated_hold_hours_low:g}–{signal.trade.estimated_hold_hours_high:g}h"
    invalidation = signal.trade.invalidation_reason
    if signal.trade.invalidation_level is not None:
        relation = "below" if signal.direction is Direction.LONG else "above"
        invalidation = f"1H close {relation} {_price(signal.trade.invalidation_level)}"
    base_asset = signal.symbol.split("/", maxsplit=1)[0]
    simulations = [simulate_leverage(signal, 5_000, leverage) for leverage in (2, 5)]
    simulation_text = "\n".join(
        (
            f"{item.leverage}× · ${item.notional_usd:,.0f} notional · {item.quantity:.4f} {base_asset}\n"
            f"SL {_signed_money(item.stop_pnl_usd)} · TP1 {_signed_money(item.tp1_pnl_usd)} · TP2 {_signed_money(item.tp2_pnl_usd)}"
        )
        for item in simulations
    )
    tp3_line = f"TP3: {_price(signal.trade.tp3)}\n" if signal.trade.tp3 is not None else ""
    return (
        f"{icon} *{signal.symbol} · {signal.direction.value}*\n"
        f"*{strategy}* · {signal.grade.value}\n"
        f"{signal.regime.value.replace('_', ' ').title()} · Confluence {signal.score}/100"
        f"{supporting}\n\n"
        "🎯 *Trade Plan*\n"
        f"Entry: {_price(signal.trade.entry_zone_low)} – {_price(signal.trade.entry_zone_high)}\n"
        f"Preferred: {_price(signal.trade.preferred_entry)}\n"
        f"Stop: {_price(signal.trade.stop_loss)}\n"
        f"TP1: {_price(signal.trade.tp1)}\n"
        f"TP2 (2R): {_price(signal.trade.tp2)}\n"
        f"{tp3_line}"
        f"R:R  1:{signal.trade.reward_risk:.2f} · Hold estimate {hold_time}\n\n"
        "🔎 *Why It Qualifies*\n"
        f"{evidence}\n\n"
        "🧮 *$5,000 Margin Example*\n"
        f"{simulation_text}\n\n"
        "🛑 *Invalidation*\n"
        f"{invalidation}\n\n"
        "⚠️ Research example only. Excludes fees, funding, slippage and liquidation mechanics."
    )


def format_watch(signal: Signal) -> str:
    return (
        f"🟡 *{signal.symbol} · WATCH*\n"
        f"*{signal.strategy.replace('_', ' ').title()}* · Confluence {signal.score}/100\n\n"
        "🔎 *Confirmation Needed*\n"
        f"{signal.trade.trigger}\n\n"
        f"🛑 *Invalidation*\n{_price(signal.trade.stop_loss)}"
    )


def format_lifecycle(signal: Signal) -> str:
    icons = {"ACTIVE": "🚀", "TP1_HIT": "🎯", "TP2_HIT": "✅", "STOPPED": "🛑", "INVALIDATED": "⚠️", "EXPIRED": "⌛"}
    icon = icons.get(signal.state.value, "ℹ️")
    return f"{icon} *{signal.symbol} · {signal.direction.value}*\n{signal.state.value.replace('_', ' ').title()} · {signal.strategy.replace('_', ' ').title()}"


def _cadence(settings: Settings) -> str:
    minutes = settings.scan_interval_seconds / 60
    return f"{minutes:g} minutes"


def format_start(settings: Settings) -> str:
    watch_alerts = "enabled" if settings.send_watch_alerts else "disabled"
    watchlist = ", ".join(settings.watchlist)
    exchange = "Binance USD-M Futures" if settings.exchange == "binance" else "Bybit Linear Futures"
    return (
        "🤖 *Prism Signal Bot is online*\n\n"
        f"*Exchange*\n{exchange}\n"
        f"*Scan Cadence*\nEvery {_cadence(settings)}\n"
        f"*Watchlist*\n{watchlist}\n"
        f"*Signal Policy*\nVALID and EXCEPTIONAL alerts; WATCH alerts {watch_alerts}.\n\n"
        "The scanner runs automatically. Pressing Start does not force a trade or an immediate alert.\n"
        "Use /status to check service and scanner health, or tap Run Manual Scan below."
    )


def format_status(settings: Settings, health: RuntimeHealth) -> str:
    healthy = health.healthy
    icon = "🟢" if healthy else "🟠"
    last_scan = (
        datetime.fromtimestamp(health.last_scan_ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        if health.last_scan_ms is not None
        else "No completed scan yet"
    )
    delivery = "DRY RUN — Telegram signal delivery disabled" if settings.dry_run else "Telegram delivery enabled"
    watch_alerts = "enabled" if settings.send_watch_alerts else "disabled"
    return (
        f"{icon} *Prism Bot Status*\n\n"
        f"*Service*\n{'Healthy' if healthy else 'Starting or degraded'}\n"
        f"*Scanner*\n{health.scanner.title()}\n"
        f"*Exchange*\n{health.exchange.title()}\n"
        f"*Delivery*\n{delivery}\n"
        f"*Scan Cadence*\nEvery {_cadence(settings)}\n"
        f"*WATCH Alerts*\n{watch_alerts.title()}\n"
        f"*Last Completed Scan*\n{last_scan}\n"
        f"*Symbols Completed*\n{health.scanned_symbols}/{len(settings.watchlist)}\n"
        f"*Last Scan Errors*\n{health.last_scan_errors}\n"
        f"*Cumulative Scan Errors*\n{health.scan_errors}"
        + (f"\n*Latest Error*\n{health.last_error.replace('_', ' ')[:240]}" if health.last_error else "")
    )
