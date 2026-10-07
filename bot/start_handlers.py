from aiogram import Router, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

import dashboard
from helpers import get_or_create_user, get_active_wallet, main_reply_kb, onboarding_kb, edit

router = Router()

WELCOME_TEXT = (
    "👋 <b>Welcome to ShhhToshi</b>\n\n"
    "Trade TON tokens directly from Telegram — swap, buy and sell with a few taps.\n\n"
    "🔐 Your seed phrase is <b>encrypted and stored securely</b> so the bot can "
    "trade on your behalf. Never share your seed phrase with anyone else, "
    "and only import wallets you're comfortable trading through a bot.\n\n"
    "Let's get you set up."
)

WELCOME_BACK_TEXT = (
    "👋 <b>Welcome back to ShhhToshi</b>\n\n"
    "Use the menu below — or just paste any token contract address to see its "
    "price, liquidity and holders and trade it."
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    referral_code = command.args.strip() if command.args else None
    await get_or_create_user(message.from_user, referred_by_code=referral_code)
    wallet = await get_active_wallet(message.from_user.id)
    if wallet:
        await message.answer("👋 Welcome back to <b>ShhhToshi</b>", reply_markup=main_reply_kb())
        await dashboard.send_dashboard(message)
    else:
        # new user: ask them to create or import a wallet first; the dashboard follows
        await message.answer(dashboard.WELCOME_TEXT, reply_markup=onboarding_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    await dashboard.send_dashboard(message)


@router.callback_query(F.data == "menu:home")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    """Target of every Cancel / Back / Home button: drop any half-finished flow, show the dashboard."""
    await state.clear()
    await callback.answer()
    await dashboard.send_dashboard(callback, replace=True)
