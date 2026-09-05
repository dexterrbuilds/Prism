from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.api.health import RuntimeHealth
from app.config import Settings
from app.models import Signal, SignalGrade
from app.signals.outcomes import PerformanceStats
from app.telegram.formatter import format_lifecycle, format_signal, format_start, format_stats, format_status, format_watch

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramPublishResult:
    delivered_destination_ids: tuple[str, ...]
    failed_destination_ids: tuple[str, ...]
    message_ids: tuple[tuple[str, str], ...] = ()
    errors: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.delivered_destination_ids) and not self.failed_destination_ids

    def message_id_for(self, destination: str) -> str | None:
        return next((message_id for target, message_id in self.message_ids if target == destination), None)


class TelegramService:
    def __init__(self, settings: Settings, health: RuntimeHealth) -> None:
        self._settings = settings
        self._health = health
        self._application: Application[Any, Any, Any, Any, Any, Any] | None = None
        self._initialized = False
        self._started = False
        self._polling = False
        self._manual_scan_callback: Callable[[], bool] | None = None
        self._stats_callback: Callable[[int | None], Awaitable[PerformanceStats]] | None = None
        if settings.telegram_bot_token:
            self._application = Application.builder().token(settings.telegram_bot_token).build()
            self._application.add_handler(CommandHandler("start", self._handle_start))
            self._application.add_handler(CommandHandler("status", self._handle_status))
            self._application.add_handler(CommandHandler("stats", self._handle_stats))
            self._application.add_handler(CallbackQueryHandler(self._handle_manual_scan, pattern=r"^manual_scan$"))

    def bind_manual_scan(self, callback: Callable[[], bool]) -> None:
        self._manual_scan_callback = callback

    def bind_stats(self, callback: Callable[[int | None], Awaitable[PerformanceStats]]) -> None:
        self._stats_callback = callback

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

    async def _handle_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or update.effective_message is None:
            logger.warning("telegram_command_rejected command=stats")
            return
        if self._stats_callback is None:
            await update.effective_message.reply_text("Performance tracking is not ready yet.")
            return
        period_days: int | None = None
        if context.args:
            value = context.args[0].strip().lower()
            if value != "all":
                if not value.endswith("d") or not value[:-1].isdigit() or int(value[:-1]) < 1:
                    await update.effective_message.reply_text("Usage: /stats, /stats 7d, /stats 30d, or /stats all")
                    return
                period_days = min(3650, int(value[:-1]))
        try:
            stats = await self._stats_callback(period_days)
        except Exception as exc:
            logger.warning("telegram_stats_failure error=%s", type(exc).__name__)
            await update.effective_message.reply_text("Performance statistics are temporarily unavailable. Please try again shortly.")
            return
        await update.effective_message.reply_text(format_stats(stats), parse_mode=ParseMode.MARKDOWN)

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
                    BotCommand("stats", "Show tracked signal win rate"),
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

    async def publish(
        self,
        signal: Signal,
        lifecycle: bool = False,
        chart_png: bytes | None = None,
        *,
        destinations: tuple[str, ...] | None = None,
    ) -> TelegramPublishResult:
        try:
            text = (
                format_lifecycle(signal)
                if lifecycle
                else format_watch(signal)
                if signal.grade is SignalGrade.WATCH
                else format_signal(signal)
            )
            if not text or len(text) > 4096:
                raise ValueError(f"Telegram message length is invalid: {len(text)}")
        except Exception as exc:
            logger.exception(
                "telegram_format_failure signal_id=%s lifecycle=%s error=%s message=%s",
                signal.id,
                lifecycle,
                type(exc).__name__,
                str(exc)[:300],
            )
            targets = destinations if destinations is not None else self._settings.telegram_delivery_ids
            return TelegramPublishResult((), targets, errors=(f"{type(exc).__name__}: {str(exc)[:240]}",))
        if self._settings.dry_run:
            targets = destinations if destinations is not None else self._settings.telegram_delivery_ids
            logger.info(
                "telegram_dry_run signal_id=%s symbol=%s strategy=%s score=%d state=%s recipients=%d",
                signal.id,
                signal.symbol,
                signal.strategy,
                signal.score,
                signal.state.value,
                len(targets),
            )
            return TelegramPublishResult(targets, ())
        targets = destinations if destinations is not None else self._settings.telegram_delivery_ids
        if self._application is None or not targets:
            logger.error("telegram_failure signal_id=%s reason=not_configured", signal.id)
            return TelegramPublishResult((), targets, errors=("Telegram is not configured",))

        delivered: list[str] = []
        failed: list[str] = []
        message_ids: list[tuple[str, str]] = []
        errors: list[str] = []
        for destination in targets:
            message_id, error = await self._send_with_retry(
                signal,
                destination,
                text,
                lifecycle=lifecycle,
                chart_png=chart_png,
            )
            if error is None:
                delivered.append(destination)
                if message_id is not None:
                    message_ids.append((destination, message_id))
                destination_type = "channel" if destination in self._settings.telegram_channel_ids else "dm"
                logger.info(
                    "telegram_destination_success signal_id=%s destination_type=%s destination=%s message_id=%s",
                    signal.id,
                    destination_type,
                    destination,
                    message_id or "unavailable",
                )
            else:
                failed.append(destination)
                errors.append(f"{destination}: {error}")
        return TelegramPublishResult(tuple(delivered), tuple(failed), tuple(message_ids), tuple(errors))

    async def _send_with_retry(
        self,
        signal: Signal,
        destination: str,
        text: str,
        *,
        lifecycle: bool,
        chart_png: bytes | None,
    ) -> tuple[str | None, str | None]:
        assert self._application is not None
        plain_text = text.replace("*", "").replace("`", "")
        use_plain_text = False
        for attempt in range(1, 4):
            try:
                parse_mode = None if use_plain_text else ParseMode.MARKDOWN
                message: Any
                if chart_png and len(text) <= 1024:
                    chart = BytesIO(chart_png)
                    suffix = signal.state.value.lower() if lifecycle else signal.strategy.lower()
                    chart.name = f"{signal.symbol.replace('/', '-')}-{suffix}.png"
                    message = await self._application.bot.send_photo(
                        chat_id=destination,
                        photo=chart,
                        caption=plain_text if use_plain_text else text,
                        parse_mode=parse_mode,
                    )
                else:
                    # Send the actionable text first.  A chart failure after a
                    # successful text must not cause a duplicate text retry.
                    message = await self._application.bot.send_message(
                        chat_id=destination,
                        text=plain_text if use_plain_text else text,
                        parse_mode=parse_mode,
                        disable_web_page_preview=True,
                    )
                    if chart_png:
                        try:
                            chart = BytesIO(chart_png)
                            chart.name = f"{signal.symbol.replace('/', '-')}-analysis.png"
                            await self._application.bot.send_photo(
                                chat_id=destination,
                                photo=chart,
                                caption=f"{signal.symbol} — {signal.direction.value} analysis chart",
                            )
                        except Exception as chart_exc:
                            logger.warning(
                                "telegram_chart_send_failure signal_id=%s destination=%s error=%s message=%s",
                                signal.id,
                                destination,
                                type(chart_exc).__name__,
                                str(chart_exc)[:300],
                            )
                raw_message_id = getattr(message, "message_id", None)
                return str(raw_message_id) if raw_message_id is not None else None, None
            except BadRequest as exc:
                detail = str(exc).replace("\n", " ")[:300]
                if "parse" in detail.lower() and not use_plain_text:
                    use_plain_text = True
                    logger.warning(
                        "telegram_parse_fallback signal_id=%s destination=%s error=%s",
                        signal.id,
                        destination,
                        detail,
                    )
                    continue
                logger.warning(
                    "telegram_send_failure signal_id=%s destination=%s attempt=%d error=%s message=%s",
                    signal.id,
                    destination,
                    attempt,
                    type(exc).__name__,
                    detail,
                )
                return None, f"{type(exc).__name__}: {detail}"
            except Exception as exc:
                detail = str(exc).replace("\n", " ")[:300]
                logger.warning(
                    "telegram_send_failure signal_id=%s destination=%s attempt=%d error=%s message=%s",
                    signal.id,
                    destination,
                    attempt,
                    type(exc).__name__,
                    detail,
                    exc_info=attempt == 3,
                )
                if attempt == 3:
                    return None, f"{type(exc).__name__}: {detail}"
                retry_after = getattr(exc, "retry_after", None) if isinstance(exc, RetryAfter) else None
                if retry_after is not None and hasattr(retry_after, "total_seconds"):
                    delay = float(retry_after.total_seconds())
                else:
                    delay = float(retry_after or 2 ** (attempt - 1))
                await asyncio.sleep(min(8.0, max(0.25, delay)))
        return None, "Telegram retry loop exhausted"
