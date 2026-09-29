from aiogram import Router, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

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
        await message.answer(WELCOME_BACK_TEXT, reply_markup=main_reply_kb())
    else:
        await message.answer(WELCOME_TEXT, reply_markup=onboarding_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    wallet = await get_active_wallet(message.from_user.id)
    if wallet:
        await message.answer("Menu ready 👇", reply_markup=main_reply_kb())
    else:
        await message.answer("Create or import a wallet first:", reply_markup=onboarding_kb())


@router.callback_query(F.data == "menu:home")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    """Target of every Cancel / Back button: drop any half-finished flow."""
    await state.clear()
    await callback.answer()
    await edit(callback.message, "🏠 Use the menu buttons below.")
