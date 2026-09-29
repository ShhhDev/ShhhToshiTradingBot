import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from config import config
from db import init_db
import menu_handlers
import start_handlers
import wallet_handlers
import balance_handlers
import deposit_handlers
import settings_handlers
import trade_handlers
import referral_handlers
import admin_handlers
from deposit_watcher import run_deposit_watcher
from helpers import esc, is_admin

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def on_error(event: ErrorEvent) -> bool:
    """Safety net: any unhandled exception is logged AND the user gets a message,
    instead of the button silently doing nothing."""
    logger.error("Unhandled error while processing an update", exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query:
            cb = update.callback_query
            await cb.answer("⚠️ Something went wrong. Please try again.", show_alert=True)
            user_id = cb.from_user.id
        elif update.message:
            user_id = update.message.from_user.id
            text = "⚠️ Something went wrong. Please try again."
            if is_admin(user_id):
                text += f"\n\n<i>Admin detail:</i> <code>{esc(repr(event.exception))[:300]}</code>"
            await update.message.answer(text)
    except Exception:
        pass
    return True


async def main():
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in .env")

    await init_db()

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.errors.register(on_error)

    # Order matters: the menu router goes first so a menu tap always wins over any
    # half-finished flow (typing an amount, pasting an address, ...).
    dp.include_router(menu_handlers.router)
    dp.include_router(start_handlers.router)
    dp.include_router(wallet_handlers.router)
    dp.include_router(balance_handlers.router)
    dp.include_router(deposit_handlers.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(trade_handlers.router)
    dp.include_router(referral_handlers.router)
    dp.include_router(admin_handlers.router)

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Start / main menu"),
            BotCommand(command="menu", description="Show the menu buttons"),
            BotCommand(command="cancel", description="Cancel the current action"),
        ])
    except Exception as e:
        logger.warning(f"could not set bot commands: {e}")

    watcher_task = asyncio.create_task(run_deposit_watcher(bot))
    try:
        await dp.start_polling(bot)
    finally:
        watcher_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
