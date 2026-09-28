from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from helpers import get_or_create_user, get_active_wallet, main_reply_kb, onboarding_kb, edit, main_menu_kb

router = Router()

WELCOME_TEXT = (
    "👋 <b>Welcome to ShhhToshi</b>\n\n"
    "Trade TON tokens directly from Telegram — buy, sell, and swap in a few taps.\n\n"
    "🔐 Your seed phrase is encrypted and stored securely so the bot can trade "
    "on your behalf. Never share it with anyone.\n\n"
    "Create or import a wallet to get started."
)

WELCOME_BACK_TEXT = (
    "👋 <b>Welcome back to ShhhToshi</b>\n\n"
    "Trade any TON token from this chat.\n\n"
    "• Tap <b>Balance</b> to see your holdings\n"
    "• Tap <b>Deposit</b> for your wallet address\n"
    "• Tap <b>Swap</b> to trade\n"
    "• Paste any token contract address to open its card with <b>Buy</b> / <b>Sell</b>"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await get_or_create_user(message.from_user)
    wallet = await get_active_wallet(message.from_user.id)
    if wallet:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 Balance", callback_data="bal:refresh"),
                InlineKeyboardButton(text="📥 Deposit", callback_data="dep:home"),
            ],
            [
                InlineKeyboardButton(text="🔁 Swap", callback_data="swap:start"),
                InlineKeyboardButton(text="⚙️ Settings", callback_data="set:home"),
            ],
        ])
        await message.answer(WELCOME_BACK_TEXT, reply_markup=main_reply_kb())
        await message.answer("Quick actions:", reply_markup=kb)
    else:
        await message.answer(WELCOME_TEXT, reply_markup=onboarding_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    wallet = await get_active_wallet(message.from_user.id)
    if wallet:
        await message.answer(
            "Choose an action below.",
            reply_markup=main_reply_kb(),
        )
    else:
        await message.answer("Create or import a wallet first:", reply_markup=onboarding_kb())


@router.callback_query(F.data == "menu:home")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await edit(callback.message, "Choose an action from the buttons below.")
    try:
        await callback.message.answer("Menu:", reply_markup=main_reply_kb())
    except Exception:
        pass
