import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from db import init_db
import start_handlers
import wallet_handlers
import balance_handlers
import trade_handlers
import settings_handlers
import admin_handlers

logging.basicConfig(level=logging.INFO)


async def main():
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in .env")

    await init_db()

    bot = Bot(token=config.BOT_TOKEN, parse_mode="HTML")
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(start_handlers.router)
    dp.include_router(wallet_handlers.router)
    dp.include_router(balance_handlers.router)
    dp.include_router(trade_handlers.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(admin_handlers.router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
