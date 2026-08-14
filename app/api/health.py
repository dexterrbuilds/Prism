from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI


@dataclass(slots=True)
class RuntimeHealth:
    exchange: str
    scanner: str = "starting"
    last_scan_ms: int | None = None
    scanned_symbols: int = 0
    scan_errors: int = 0


def create_app(health: RuntimeHealth) -> FastAPI:
    app = FastAPI(title="Prism Signal Engine", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def get_health() -> dict[str, str | int | None]:
        status = "ok" if health.scanner in {"running", "sleeping"} else "degraded"
        return {
            "status": status,
            "scanner": health.scanner,
            "exchange": health.exchange,
            "last_scan_ms": health.last_scan_ms,
            "scanned_symbols": health.scanned_symbols,
            "scan_errors": health.scan_errors,
        }

    return app
