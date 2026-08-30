from __future__ import annotations

import httpx
import pytest

from app.api.health import RuntimeHealth, create_app


@pytest.mark.asyncio
async def test_health_endpoint_reports_runtime_state() -> None:
    health = RuntimeHealth("binance", scanner="running", last_scan_ms=123, scanned_symbols=5)
    transport = httpx.ASGITransport(app=create_app(health))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "scanner": "running",
        "exchange": "binance",
        "ai": "disabled",
        "scalp": "disabled",
        "last_scan_ms": 123,
        "scanned_symbols": 5,
        "scan_errors": 0,
        "last_scan_errors": 0,
        "last_error": None,
    }


@pytest.mark.asyncio
async def test_health_is_degraded_when_latest_scan_failed() -> None:
    health = RuntimeHealth(
        "binance",
        scanner="sleeping",
        last_scan_ms=123,
        scanned_symbols=0,
        scan_errors=5,
        last_scan_errors=5,
        last_error="BTC/USDT 4h: ExchangeRequestError",
    )
    transport = httpx.ASGITransport(app=create_app(health))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.json()["status"] == "degraded"
