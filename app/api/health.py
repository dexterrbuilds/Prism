from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI


@dataclass(slots=True)
class RuntimeHealth:
    exchange: str
    ai: str = "disabled"
    scalp: str = "disabled"
    scanner: str = "starting"
    last_scan_ms: int | None = None
    scanned_symbols: int = 0
    scan_errors: int = 0
    last_scan_errors: int = 0
    last_error: str | None = None
    candidates_detected: int = 0
    signals_issued: int = 0
    duplicate_candidates_suppressed: int = 0
    reentries_issued: int = 0
    same_symbol_concurrent_signals: int = 0
    orphaned_signals_reconciled: int = 0
    publication_successes: int = 0
    publication_failures: int = 0
    dm_delivery_failures: int = 0
    lifecycle_notifications_suppressed: int = 0

    @property
    def healthy(self) -> bool:
        scanner_operational = self.scanner in {"running", "sleeping"}
        last_scan_usable = self.last_scan_ms is None or self.last_scan_errors == 0
        return scanner_operational and last_scan_usable


def create_app(health: RuntimeHealth) -> FastAPI:
    app = FastAPI(title="Prism Signal Engine", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def get_health() -> dict[str, str | int | None]:
        status = "ok" if health.healthy else "degraded"
        return {
            "status": status,
            "scanner": health.scanner,
            "exchange": health.exchange,
            "ai": health.ai,
            "scalp": health.scalp,
            "last_scan_ms": health.last_scan_ms,
            "scanned_symbols": health.scanned_symbols,
            "scan_errors": health.scan_errors,
            "last_scan_errors": health.last_scan_errors,
            "last_error": health.last_error,
            "candidates_detected": health.candidates_detected,
            "signals_issued": health.signals_issued,
            "duplicate_candidates_suppressed": health.duplicate_candidates_suppressed,
            "reentries_issued": health.reentries_issued,
            "same_symbol_concurrent_signals": health.same_symbol_concurrent_signals,
            "orphaned_signals_reconciled": health.orphaned_signals_reconciled,
            "publication_successes": health.publication_successes,
            "publication_failures": health.publication_failures,
            "dm_delivery_failures": health.dm_delivery_failures,
            "lifecycle_notifications_suppressed": health.lifecycle_notifications_suppressed,
        }

    return app
