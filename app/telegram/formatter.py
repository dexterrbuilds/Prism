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
    healthy = health.scanner in {"running", "sleeping"}
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
        f"*Cumulative Scan Errors*\n{health.scan_errors}"
    )
