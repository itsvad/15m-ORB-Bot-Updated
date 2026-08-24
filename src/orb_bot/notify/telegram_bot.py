"""Telegram integration for remote monitoring/control.

Push notifications on entry/exit/error, plus /status, /pause, /resume and
an emergency /flatten command, restricted to an explicit allow-list of
chat IDs (TELEGRAM_ALLOWED_CHAT_IDS).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from orb_bot.notify.formatting import format_alert, format_notify
from orb_bot.persistence.store import StateStore
from orb_bot.strategy.models import Alert, Notify

logger = logging.getLogger("orb_bot.telegram")

StatusProvider = Callable[[], Awaitable[str]]
FlattenCallback = Callable[[], Awaitable[str]]


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        allowed_chat_ids: list[int],
        store: StateStore,
        status_provider: StatusProvider,
        flatten_callback: FlattenCallback,
    ) -> None:
        self.token = token
        self.allowed_chat_ids = set(allowed_chat_ids)
        self.store = store
        self.status_provider = status_provider
        self.flatten_callback = flatten_callback
        self.app: Application | None = None

    def _authorized(self, update: Update) -> bool:
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id in self.allowed_chat_ids:
            return True
        logger.warning("Rejected Telegram command from unauthorized chat_id=%s", chat_id)
        return False

    async def _status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        text = await self.status_provider()
        await update.message.reply_text(text)

    async def _pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self.store.set_paused(True)
        await update.message.reply_text(
            "⏸ Trading PAUSED. No new entry orders will be placed until /resume. "
            "Any already-open position keeps being managed normally (stops, TP1, hard close)."
        )

    async def _resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self.store.set_paused(False)
        await update.message.reply_text("▶ Trading RESUMED.")

    async def _flatten(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text("\U0001F6A8 Emergency flatten requested...")
        result = await self.flatten_callback()
        await update.message.reply_text(result)

    async def start(self) -> None:
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("status", self._status))
        self.app.add_handler(CommandHandler("pause", self._pause))
        self.app.add_handler(CommandHandler("resume", self._resume))
        self.app.add_handler(CommandHandler("flatten", self._flatten))
        await self.app.initialize()
        await self.app.start()
        assert self.app.updater is not None
        await self.app.updater.start_polling()
        logger.info("Telegram bot polling started")

    async def stop(self) -> None:
        if self.app is None:
            return
        if self.app.updater is not None:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    async def send_text(self, text: str) -> None:
        if self.app is None:
            logger.info("[telegram not started] %s", text)
            return
        for chat_id in self.allowed_chat_ids:
            try:
                await self.app.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                logger.exception("Failed to send Telegram message to chat_id=%s", chat_id)

    async def notify(self, action: Notify | Alert) -> None:
        if isinstance(action, Alert):
            await self.send_text(format_alert(action))
        elif isinstance(action, Notify):
            await self.send_text(format_notify(action))
