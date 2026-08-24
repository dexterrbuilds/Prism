from __future__ import annotations

import logging
from collections.abc import Callable
from io import BytesIO
from typing import Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.api.health import RuntimeHealth
from app.config import Settings
from app.models import Signal, SignalGrade
from app.telegram.formatter import format_lifecycle, format_signal, format_start, format_status, format_watch

logger = logging.getLogger(__name__)


class TelegramService:
    def __init__(self, settings: Settings, health: RuntimeHealth) -> None:
        self._settings = settings
        self._health = health
        self._application: Application[Any, Any, Any, Any, Any, Any] | None = None
        self._initialized = False
        self._started = False
        self._polling = False
        self._manual_scan_callback: Callable[[], bool] | None = None
        if settings.telegram_bot_token:
            self._application = Application.builder().token(settings.telegram_bot_token).build()
            self._application.add_handler(CommandHandler("start", self._handle_start))
            self._application.add_handler(CommandHandler("status", self._handle_status))
            self._application.add_handler(CallbackQueryHandler(self._handle_manual_scan, pattern=r"^manual_scan$"))

    def bind_manual_scan(self, callback: Callable[[], bool]) -> None:
        self._manual_scan_callback = callback

    @staticmethod
    def _manual_scan_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(((InlineKeyboardButton("🔄 Run Manual Scan", callback_data="manual_scan"),),))

    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return bool(chat and str(chat.id) in self._settings.telegram_chat_ids)

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._authorized(update) or update.effective_message is None:
            logger.warning("telegram_command_rejected command=start")
            return
        await update.effective_message.reply_text(
            format_start(self._settings),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._manual_scan_keyboard(),
        )

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._authorized(update) or update.effective_message is None:
            logger.warning("telegram_command_rejected command=status")
            return
        await update.effective_message.reply_text(
            format_status(self._settings, self._health),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._manual_scan_keyboard(),
        )

    async def _handle_manual_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        if query is None:
            return
        if not self._authorized(update):
            logger.warning("telegram_command_rejected command=manual_scan")
            await query.answer("This control is not authorized for this chat.", show_alert=True)
            return
        if self._manual_scan_callback is None:
            await query.answer("Scanner is not ready yet.", show_alert=False)
            return
        accepted = self._manual_scan_callback()
        if accepted:
            await query.answer("Manual scan requested.", show_alert=False)
            message = update.effective_message
            if message is not None:
                await message.reply_text("🔄 Manual watchlist scan requested. Use /status to follow its progress.")
            logger.info("manual_scan_requested")
        else:
            await query.answer("A scan is already running.", show_alert=False)

    async def start(self) -> None:
        if self._application is None:
            logger.info("telegram_disabled reason=no_token")
            return
        await self._application.initialize()
        self._initialized = True
        try:
            await self._application.bot.set_my_commands(
                (
                    BotCommand("start", "Show bot configuration and usage"),
                    BotCommand("status", "Check scanner and service health"),
                )
            )
        except Exception as exc:
            logger.warning("telegram_command_menu_failure error=%s", type(exc).__name__)
        await self._application.start()
        self._started = True
        if self._application.updater:
            await self._application.updater.start_polling(drop_pending_updates=False)
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

    async def publish(self, signal: Signal, lifecycle: bool = False, chart_png: bytes | None = None) -> bool:
        text = format_lifecycle(signal) if lifecycle else format_watch(signal) if signal.grade is SignalGrade.WATCH else format_signal(signal)
        if self._settings.dry_run:
            logger.info(
                "telegram_dry_run symbol=%s strategy=%s score=%d state=%s recipients=%d",
                signal.symbol,
                signal.strategy,
                signal.score,
                signal.state.value,
                len(self._settings.telegram_chat_ids),
            )
            return True
        if self._application is None or not self._settings.telegram_chat_ids:
            logger.error("telegram_failure reason=not_configured")
            return False
        successes = 0
        for chat_id in self._settings.telegram_chat_ids:
            try:
                if chart_png and not lifecycle:
                    chart = BytesIO(chart_png)
                    chart.name = f"{signal.symbol.replace('/', '-')}-{signal.strategy.lower()}.png"
                    if len(text) <= 1024:
                        await self._application.bot.send_photo(chat_id=chat_id, photo=chart, caption=text, parse_mode=ParseMode.MARKDOWN)
                    else:
                        await self._application.bot.send_photo(
                            chat_id=chat_id,
                            photo=chart,
                            caption=f"{signal.symbol} — {signal.direction.value} analysis chart",
                        )
                        await self._application.bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=True,
                        )
                else:
                    await self._application.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True,
                    )
                successes += 1
                logger.info("telegram_success symbol=%s strategy=%s state=%s", signal.symbol, signal.strategy, signal.state.value)
            except Exception as exc:
                logger.warning("telegram_failure symbol=%s error=%s", signal.symbol, type(exc).__name__)
        return successes == len(self._settings.telegram_chat_ids)
