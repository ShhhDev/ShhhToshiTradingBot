from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from sqlalchemy import select

from db import async_session, User, Wallet

router = Router()


def main_menu_kb(has_wallet: bool) -> InlineKeyboardMarkup:
    if not has_wallet:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✨ Create Wallet", callback_data="wallet:create")],
            [InlineKeyboardButton(text="📥 Import Wallet", callback_data="wallet:import")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Balance", callback_data="menu:balance"),
            InlineKeyboardButton(text="🔁 Swap", callback_data="menu:swap"),
        ],
        [
            InlineKeyboardButton(text="🟢 Buy", callback_data="menu:buy"),
            InlineKeyboardButton(text="🔴 Sell", callback_data="menu:sell"),
        ],
        [InlineKeyboardButton(text="⚙️ Settings", callback_data="menu:settings")],
    ])


WELCOME_TEXT = (
    "👋 <b>Welcome to ShhhToshi</b>\n\n"
    "Trade TON tokens directly from Telegram — buy, sell, and swap with one tap.\n\n"
    "🔐 Your seed phrase is <b>encrypted and stored securely</b> so the bot can "
    "trade on your behalf. Never share your seed phrase with anyone else, "
    "and only import wallets you're comfortable trading through a bot.\n\n"
    "Let's get you set up."
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=message.from_user.id, username=message.from_user.username)
            session.add(user)
            await session.commit()
            await session.refresh(user)

        wallet_result = await session.execute(select(Wallet).where(Wallet.user_id == user.id))
        has_wallet = wallet_result.scalar_one_or_none() is not None

    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb(has_wallet))


@router.callback_query(F.data == "menu:home")
async def back_to_menu(callback: CallbackQuery):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = result.scalar_one()
        wallet_result = await session.execute(select(Wallet).where(Wallet.user_id == user.id))
        has_wallet = wallet_result.scalar_one_or_none() is not None

    await callback.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb(has_wallet))
    await callback.answer()
