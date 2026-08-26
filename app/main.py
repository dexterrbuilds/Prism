from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

import uvicorn

from app.api.health import RuntimeHealth, create_app
from app.config import Settings
from app.exchange.client import ExchangeClient, ExchangeRequestError
from app.logging_config import configure_logging
from app.scanner import Scanner
from app.signals.repository import OutcomeRepository, create_outcome_repository
from app.telegram.bot import TelegramService

logger = logging.getLogger(__name__)


class EmbeddedServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        """The parent asyncio lifecycle owns SIGTERM/SIGINT."""


async def run() -> None:
    settings = Settings.from_env()
    settings.validate()
    configure_logging(settings.log_level)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    health = RuntimeHealth(settings.exchange)
    exchange = ExchangeClient(settings)
    outcomes: OutcomeRepository = await create_outcome_repository(settings)
    logger.info(
        "outcome_tracking_started backend=%s location=%s history_limit=%d",
        outcomes.backend,
        outcomes.location,
        settings.signal_history_limit,
    )
    if outcomes.backend == "sqlite" and outcomes.location.startswith("/tmp/"):
        logger.warning("outcome_tracking_ephemeral path=%s", outcomes.location)
    telegram = TelegramService(settings, health)
    telegram.bind_stats(outcomes.stats)
    scanner = Scanner(settings, exchange, telegram, health, outcomes)
    await scanner.restore_outcomes()
    telegram.bind_manual_scan(scanner.request_manual_scan)
    app = create_app(health)
    server = EmbeddedServer(uvicorn.Config(app, host="0.0.0.0", port=settings.port, log_level=settings.log_level.lower(), access_log=False, log_config=None))
    scanner_task: asyncio.Task[None] | None = None
    server_task = asyncio.create_task(server.serve(), name="uvicorn")
    try:
        try:
            await telegram.start()
        except Exception as exc:
            logger.warning("telegram_start_failure error=%s", type(exc).__name__)
        try:
            await exchange.load_markets()
        except ExchangeRequestError as exc:
            logger.warning("exchange_start_failure error=%s", exc)
        active_scanner_task = asyncio.create_task(scanner.run(stop_event), name="scanner")
        scanner_task = active_scanner_task
        stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-wait")
        done, _ = await asyncio.wait({active_scanner_task, server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if server_task in done and not stop_event.is_set():
            error = server_task.exception()
            if error:
                raise error
        if active_scanner_task in done and not stop_event.is_set():
            error = active_scanner_task.exception()
            if error:
                raise error
        stop_event.set()
    finally:
        logger.info("shutdown_started")
        stop_event.set()
        server.should_exit = True
        if scanner_task:
            await asyncio.gather(scanner_task, return_exceptions=True)
        if server_task:
            await asyncio.gather(server_task, return_exceptions=True)
        try:
            await telegram.stop()
        except Exception as exc:
            logger.warning("telegram_stop_failure error=%s", type(exc).__name__)
        await exchange.close()
        await outcomes.close()
        logger.info("shutdown_completed")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
