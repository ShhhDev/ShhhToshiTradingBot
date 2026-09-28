from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from helpers import get_or_create_user, get_active_wallet, main_reply_kb, onboarding_kb, edit

router = Router()

WELCOME_TEXT = (
    "👋 <b>Welcome to ShhhToshi</b>\n\n"
    "Trade TON tokens directly from Telegram — buy, sell, and swap with one tap.\n\n"
    "🔐 Your seed phrase is <b>encrypted and stored securely</b> so the bot can "
    "trade on your behalf. Never share your seed phrase with anyone else, "
    "and only import wallets you're comfortable trading through a bot.\n\n"
    "Let's get you set up."
)

WELCOME_BACK_TEXT = (
    "👋 <b>Welcome back to ShhhToshi</b>\n\n"
    "Use the <b>menu buttons</b> below — or paste any token contract address "
    "to see its price and trade it.\n\n"
    "If you only see the normal keyboard, tap the button next to the text field "
    "to switch back to the bot menu."
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await get_or_create_user(message.from_user)
    wallet = await get_active_wallet(message.from_user.id)
    if wallet:
        # Always re-attach the persistent reply menu
        await message.answer(WELCOME_BACK_TEXT, reply_markup=main_reply_kb())
    else:
        await message.answer(WELCOME_TEXT, reply_markup=onboarding_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    wallet = await get_active_wallet(message.from_user.id)
    if wallet:
        await message.answer(
            "📱 <b>Menu ready</b> — use the buttons below.",
            reply_markup=main_reply_kb(),
        )
    else:
        await message.answer("Create or import a wallet first:", reply_markup=onboarding_kb())


@router.callback_query(F.data == "menu:home")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    """Target of every Cancel / Back button: drop any half-finished flow."""
    await state.clear()
    await callback.answer()
    await edit(callback.message, "🏠 Use the menu buttons below.")
    # Re-show reply keyboard in case it was lost
    try:
        await callback.message.answer("📱 Menu:", reply_markup=main_reply_kb())
    except Exception:
        pass
