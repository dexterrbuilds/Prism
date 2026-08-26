from __future__ import annotations

from datetime import UTC, datetime

from app.api.health import RuntimeHealth
from app.config import Settings
from app.models import Direction, Signal
from app.signals.outcomes import PerformanceStats
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


def _current_price(signal: Signal) -> float:
    return signal.current_price if signal.current_price is not None else signal.trade.preferred_entry


def _hold_time(signal: Signal) -> str:
    if signal.activated_at is None or signal.state_changed_at is None:
        return "Not available"
    hours = max(0.0, (signal.state_changed_at - signal.activated_at).total_seconds() / 3600)
    if hours < 1:
        return f"{max(1, round(hours * 60))}m"
    return f"{hours:.1f}h"


def _duration(minutes: int | None) -> str:
    if minutes is None:
        return "Not configured"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} HOUR" if hours == 1 else f"{hours} HOURS"
    if minutes > 60:
        return f"{minutes // 60}H {minutes % 60}M"
    return f"{minutes} MINUTES"


def _elapsed(signal: Signal) -> str:
    event_at = signal.state_changed_at or signal.created_at
    seconds = max(0.0, (event_at - signal.created_at).total_seconds())
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes / 60:.1f}h"


def _expiry(signal: Signal) -> str:
    if signal.expires_at is None:
        return "Not configured"
    return signal.expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _targets(signal: Signal) -> str:
    lines = [
        f"TP1: {_price(signal.trade.tp1)}",
        f"TP2: {_price(signal.trade.tp2)} · 2R",
    ]
    if signal.trade.tp3 is not None:
        lines.append(f"TP3: {_price(signal.trade.tp3)}")
    if signal.trade.tp4 is not None:
        lines.append(f"TP4: {_price(signal.trade.tp4)}")
    return "\n".join(lines)


def format_signal(signal: Signal) -> str:
    evidence = "\n".join(f"• {item[:60]}" for item in signal.evidence[:3])
    strategy = signal.strategy.replace("_", " ").title()
    supporting = ""
    if signal.supporting_strategies:
        names = " · ".join(name.replace("_", " ").title() for name in signal.supporting_strategies[:2])
        supporting = f"\nSupports: {names}"
    base_asset = signal.symbol.split("/", maxsplit=1)[0]
    simulations = [simulate_leverage(signal, 5_000, leverage) for leverage in (2, 5)]
    simulation_text = "\n".join(
        (
            f"{item.leverage}× ${item.notional_usd:,.0f} · {item.quantity:.4f} {base_asset} · "
            f"SL {_signed_money(item.stop_pnl_usd)} / TP1 {_signed_money(item.tp1_pnl_usd)} / TP2 {_signed_money(item.tp2_pnl_usd)}"
        )
        for item in simulations
    )
    conditions = "\n".join(f"• {condition[:78]}" for condition in signal.valid_conditions[:3])
    if not conditions:
        conditions = f"• {signal.trade.invalidation_reason[:78]}"
    invalidation_level = signal.trade.invalidation_level or signal.trade.stop_loss
    return (
        f"🚨 *{signal.symbol} · {signal.direction.value}*\n"
        "*SETUP DETECTED · LIVE SETUP*\n"
        f"{strategy} · {signal.grade.value} · Confluence {signal.score}/100"
        f"{supporting}\n\n"
        f"📍 *Current Price*  {_price(_current_price(signal))}\n\n"
        "🎯 *ENTRY*\n"
        f"{_price(signal.trade.entry_zone_low)} – {_price(signal.trade.entry_zone_high)}\n"
        f"Preferred: {_price(signal.trade.preferred_entry)}\n"
        f"Trigger: {signal.trade.trigger[:100]}\n\n"
        f"🛡 *INVALIDATION*  {_price(invalidation_level)}\n"
        f"{signal.trade.invalidation_reason[:100]}\n\n"
        f"🎯 *TARGETS*\n{_targets(signal)}\n"
        f"R:R 1:{signal.trade.reward_risk:.2f}\n\n"
        f"📊 *Setup*  {strategy}\n"
        f"⏱ *Timeframe*  Trade {signal.trading_timeframe.upper()} · Analysis {signal.analysis_timeframe.upper()}\n"
        f"⏳ *SETUP VALID FOR: {_duration(signal.validity_minutes)}*\n"
        f"🕐 *EXPIRES: {_expiry(signal)}*\n\n"
        f"✅ *VALID WHILE*\n{conditions}\n\n"
        f"🔎 *Confluence*\n{evidence}\n\n"
        "🧮 *$5K Margin Simulation*\n"
        f"{simulation_text}\n\n"
        "⚡ *Waiting for entry…*\n"
        "_Research only · fees, funding and slippage excluded._"
    )


def format_watch(signal: Signal) -> str:
    return (
        f"🟡 *{signal.symbol} · WATCH*\n"
        f"*{signal.strategy.replace('_', ' ').title()}* · Confluence {signal.score}/100\n\n"
        f"💹 *Current Price*\n{_price(_current_price(signal))} · latest closed 15M candle\n\n"
        "🔎 *Confirmation Needed*\n"
        f"{signal.trade.trigger}\n\n"
        f"🛑 *Invalidation*\n{_price(signal.trade.invalidation_level or signal.trade.stop_loss)}\n"
        f"⏳ Valid for {_duration(signal.validity_minutes)} · Expires {_expiry(signal)}"
    )


def format_lifecycle(signal: Signal) -> str:
    strategy = signal.strategy.replace("_", " ").title()
    price = _current_price(signal)
    move = (price - signal.trade.preferred_entry) / signal.trade.preferred_entry * 100
    if signal.direction is Direction.SHORT:
        move *= -1
    timestamp = (signal.state_changed_at or signal.created_at).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if signal.state.value in {"ACTIVE", "ENTRY_TRIGGERED"}:
        trigger_price = signal.entry_trigger_price or price
        return (
            "🟢 *ENTRY TRIGGERED*\n"
            f"*{signal.symbol} · {signal.direction.value}*\n"
            "Status: *ACTIVE*\n\n"
            f"📍 *Current Price*  {_price(price)}\n"
            f"*Actual Trigger*  {_price(trigger_price)}\n"
            f"*Entry Zone*  {_price(signal.trade.entry_zone_low)} – {_price(signal.trade.entry_zone_high)}\n"
            f"*Preferred Entry*  {_price(signal.trade.preferred_entry)}\n\n"
            f"🛡 *SL*  {_price(signal.trade.stop_loss)}\n"
            f"🎯 *TP1*  {_price(signal.trade.tp1)} · *TP2*  {_price(signal.trade.tp2)}\n"
            f"*Risk : Reward*  1:{signal.trade.reward_risk:.2f}\n\n"
            f"📊 {strategy} · {signal.trading_timeframe.upper()} trade / {signal.analysis_timeframe.upper()} analysis\n"
            f"⏱ Triggered {_elapsed(signal)} after setup creation\n"
            f"🕐 {timestamp}"
        )
    if signal.state.value in {"TP1_HIT", "TP2_HIT"}:
        tp2 = signal.state.value == "TP2_HIT"
        target = signal.trade.tp2 if tp2 else signal.trade.tp1
        achieved_r = abs(target - signal.trade.preferred_entry) / signal.trade.risk_per_unit
        title = "TP2 HIT · RUNNER COMPLETE" if tp2 else "TP1 HIT · WIN RECORDED"
        next_step = "Trade reached the planned 2R objective." if tp2 else f"Next objective: {_price(signal.trade.tp2)} · 2R"
        return (
            f"{'🏆' if tp2 else '🎯'} *{title}*\n"
            f"*{signal.symbol} · {signal.direction.value}* · {strategy}\n\n"
            f"*Target*  {_price(target)}\n"
            f"*Current Price*  {_price(price)}\n"
            f"*Move from Entry*  {move:+.2f}%\n"
            f"*Result*  +{achieved_r:.2f}R · Hold {_hold_time(signal)}\n\n"
            f"✅ {next_step}\n"
            f"{timestamp}\n\n"
            "_TP1 is counted as a win in Prism statistics._"
        )
    if signal.state.value in {"STOPPED", "SL_HIT"}:
        won = signal.tp1_hit_at is not None
        return (
            f"🛑 *{'RUNNER CLOSED' if won else 'STOP LOSS HIT'}*\n"
            f"*{signal.symbol} · {signal.direction.value}* · {strategy}\n\n"
            f"*Stop*  {_price(signal.trade.stop_loss)}\n"
            f"*Current Price*  {_price(price)}\n"
            f"*Outcome*  {'TP1 win remains recorded' if won else 'Loss recorded'}\n"
            f"*Hold*  {_hold_time(signal)} · {timestamp}"
        )
    reason = signal.lifecycle_reason or "The setup is no longer actionable."
    if signal.state.value == "MISSED":
        return (
            "⚪ *SETUP MISSED*\n"
            f"*{signal.symbol} · {signal.direction.value}*\n\n"
            f"*Entry Zone*  {_price(signal.trade.entry_zone_low)} – {_price(signal.trade.entry_zone_high)}\n"
            f"*Current Price*  {_price(price)}\n\n"
            f"*Reason*\n{reason}\n\n"
            "_No trade was activated. Not counted in win rate._"
        )
    if signal.state.value == "INVALIDATED":
        return (
            "🔴 *SETUP INVALIDATED*\n"
            f"*{signal.symbol} · {signal.direction.value}*\n\n"
            f"*Setup*  {strategy}\n"
            f"*Current Price*  {_price(price)}\n"
            f"*Invalidation Level*  {_price(signal.trade.invalidation_level or signal.trade.stop_loss)}\n\n"
            f"*Reason*\n{reason}\n\n"
            "_The setup is no longer actionable. Not counted in win rate._"
        )
    if signal.state.value == "EXPIRED":
        return (
            "⏰ *SETUP EXPIRED*\n"
            f"*{signal.symbol} · {signal.direction.value}* · {strategy}\n\n"
            f"*Entry Zone*  {_price(signal.trade.entry_zone_low)} – {_price(signal.trade.entry_zone_high)}\n"
            f"*Current Price*  {_price(price)}\n"
            f"*Created*  {signal.created_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"*Expiry*  {_expiry(signal)}\n"
            f"*Duration*  {_duration(signal.validity_minutes)}\n\n"
            f"*Reason*\n{reason}\n\n"
            "_No trade was activated. Not counted in win rate._"
        )
    return f"ℹ️ *{signal.symbol} · {signal.direction.value}*\n{signal.state.value.replace('_', ' ').title()} · {strategy}"


def format_stats(stats: PerformanceStats) -> str:
    period = f"Last {stats.period_days} days" if stats.period_days is not None else "Since tracking began"
    win_rate = f"{stats.win_rate:.1f}%" if stats.win_rate is not None else "Awaiting resolved trades"
    average_hold = f"{stats.average_hold_hours:.1f}h" if stats.average_hold_hours is not None else "Not available"
    sample_note = "\n⚠️ Small sample—do not treat this WR as calibrated." if stats.resolved < 20 else ""
    return (
        "📊 *Prism Performance*\n"
        f"{period} · TP1 counts as a win\n"
        f"Tracking from {stats.tracking_since.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"*Win Rate*  {win_rate}\n"
        f"*Resolved*  {stats.resolved} · {stats.wins}W / {stats.losses}L\n"
        f"*Activated*  {stats.activated} of {stats.signals} tracked signals\n"
        f"*TP2 Extensions*  {stats.tp2_hits}\n"
        f"*Open / TP1 Runners*  {stats.open_signals} / {stats.tp1_runners}\n"
        f"*Pre-entry Closures*  {stats.invalidated}\n"
        f"*Average Time to Outcome*  {average_hold}"
        f"{sample_note}"
    )


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
        "Use /status for service health, /stats for tracked results, or tap Run Manual Scan below."
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
    outcome_store = "Supabase / PostgreSQL" if settings.resolved_outcome_backend == "postgres" else "SQLite"
    return (
        f"{icon} *Prism Bot Status*\n\n"
        f"*Service*\n{'Healthy' if healthy else 'Starting or degraded'}\n"
        f"*Scanner*\n{health.scanner.title()}\n"
        f"*Exchange*\n{health.exchange.title()}\n"
        f"*Delivery*\n{delivery}\n"
        f"*Outcome Store*\n{outcome_store}\n"
        f"*Scan Cadence*\nEvery {_cadence(settings)}\n"
        f"*WATCH Alerts*\n{watch_alerts.title()}\n"
        f"*Last Completed Scan*\n{last_scan}\n"
        f"*Symbols Completed*\n{health.scanned_symbols}/{len(settings.watchlist)}\n"
        f"*Last Scan Errors*\n{health.last_scan_errors}\n"
        f"*Cumulative Scan Errors*\n{health.scan_errors}"
        + (f"\n*Latest Error*\n{health.last_error.replace('_', ' ')[:240]}" if health.last_error else "")
    )
