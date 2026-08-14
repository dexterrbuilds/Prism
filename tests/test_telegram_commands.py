from __future__ import annotations

from dataclasses import replace

from app.api.health import RuntimeHealth
from app.config import Settings
from app.telegram.bot import TelegramService
from app.telegram.formatter import format_start, format_status


def test_start_message_explains_automatic_scan_and_alert_policy(monkeypatch) -> None:
    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "2700")
    settings = Settings.from_env()
    message = format_start(settings)
    assert "Every 45 minutes" in message
    assert "Pressing Start does not force a trade" in message
    assert "WATCH alerts disabled" in message
    assert "/status" in message
    assert "Run Manual Scan" in message


def test_status_message_reports_runtime_health_without_secrets(monkeypatch) -> None:
    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "2700")
    settings = replace(Settings.from_env(), dry_run=False, send_watch_alerts=True)
    health = RuntimeHealth("binance", scanner="sleeping", last_scan_ms=1_800_000_000_000, scanned_symbols=5, scan_errors=1)
    message = format_status(settings, health)
    assert "Healthy" in message
    assert "Telegram delivery enabled" in message
    assert "Every 45 minutes" in message
    assert "WATCH Alerts*\nEnabled" in message
    assert "5/5" in message
    assert "token" not in message.lower()


def test_status_marks_failed_latest_scan_as_degraded(monkeypatch) -> None:
    settings = Settings.from_env()
    health = RuntimeHealth(
        "binance",
        scanner="sleeping",
        last_scan_ms=1_800_000_000_000,
        scanned_symbols=0,
        scan_errors=5,
        last_scan_errors=5,
        last_error="BTC/USDT 4h: ExchangeRequestError",
    )
    message = format_status(settings, health)
    assert "Starting or degraded" in message
    assert "Last Scan Errors*\n5" in message
    assert "Latest Error" in message


def test_manual_scan_button_callback_data_is_stable() -> None:
    keyboard = TelegramService._manual_scan_keyboard()
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "🔄 Run Manual Scan"
    assert button.callback_data == "manual_scan"
