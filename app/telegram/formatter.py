from __future__ import annotations

from datetime import UTC, datetime

from app.api.health import RuntimeHealth
from app.config import Settings
from app.models import Direction, Signal


def _price(value: float) -> str:
    if value >= 1_000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.4f}"
    return f"${value:,.6f}"


def format_signal(signal: Signal) -> str:
    icon = "🟢" if signal.direction is Direction.LONG else "🔴"
    evidence = "\n".join(f"• {item[:68]}" for item in signal.evidence[:5])
    strategy = signal.strategy.replace("_", " ").title()
    supporting = ""
    if signal.supporting_strategies:
        names = " · ".join(name.replace("_", " ").title() for name in signal.supporting_strategies[:2])
        supporting = f"\nSupports: {names}"
    hold_time = "Not calibrated"
    if signal.trade.estimated_hold_hours_low is not None and signal.trade.estimated_hold_hours_high is not None:
        hold_time = f"{signal.trade.estimated_hold_hours_low:g}–{signal.trade.estimated_hold_hours_high:g}h"
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
        f"R:R  1:{signal.trade.reward_risk:.2f} · Hold estimate {hold_time}\n\n"
        "🔎 *Why It Qualifies*\n"
        f"{evidence}\n\n"
        "🛑 *Invalidation*\n"
        f"{signal.trade.invalidation_reason}\n\n"
        "⚠️ Research signal only. Hold time is a technical estimate, not financial advice."
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
