from __future__ import annotations

import logging
from typing import Any

from telegram.constants import ParseMode
from telegram.ext import Application

from app.config import Settings
from app.models import Signal, SignalGrade
from app.telegram.formatter import format_lifecycle, format_signal, format_watch

logger = logging.getLogger(__name__)


class TelegramService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._application: Application[Any, Any, Any, Any, Any, Any] | None = None
        self._initialized = False
        self._started = False
        self._polling = False
        if settings.telegram_bot_token:
            self._application = Application.builder().token(settings.telegram_bot_token).build()

    async def start(self) -> None:
        if self._application is None:
            logger.info("telegram_disabled reason=no_token")
            return
        await self._application.initialize()
        self._initialized = True
        await self._application.start()
        self._started = True
        if self._application.updater:
            await self._application.updater.start_polling(drop_pending_updates=True)
            self._polling = True
        logger.info("telegram_started")

    async def stop(self) -> None:
        if self._application is None:
            return
        if self._application.updater and self._polling and self._application.updater.running:
            await self._application.updater.stop()
            self._polling = False
        if self._started and self._application.running:
            await self._application.stop()
            self._started = False
        if self._initialized:
            await self._application.shutdown()
            self._initialized = False
        logger.info("telegram_stopped")

    async def publish(self, signal: Signal, lifecycle: bool = False) -> bool:
        text = format_lifecycle(signal) if lifecycle else format_watch(signal) if signal.grade is SignalGrade.WATCH else format_signal(signal)
        if self._settings.dry_run:
            logger.info("telegram_dry_run symbol=%s strategy=%s score=%d state=%s", signal.symbol, signal.strategy, signal.score, signal.state.value)
            return True
        if self._application is None or not self._settings.telegram_chat_id:
            logger.error("telegram_failure reason=not_configured")
            return False
        try:
            await self._application.bot.send_message(
                chat_id=self._settings.telegram_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            logger.info("telegram_success symbol=%s strategy=%s state=%s", signal.symbol, signal.strategy, signal.state.value)
            return True
        except Exception as exc:
            logger.warning("telegram_failure symbol=%s error=%s", signal.symbol, type(exc).__name__)
            return False
