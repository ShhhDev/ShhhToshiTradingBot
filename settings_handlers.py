import asyncio

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from db import async_session, User, Wallet
import fees as fee_service
import encryption

router = Router()

SEED_AUTO_DELETE_SECONDS = 45


class ExportSeed(StatesGroup):
    waiting_for_confirm_text = State()


@router.callback_query(F.data == "menu:settings")
async def show_settings(callback: CallbackQuery):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = result.scalar_one()

    fc = await fee_service.get_fee_config()

    text = (
        "⚙️ <b>Settings</b>\n\n"
        f"Slippage tolerance: {user.slippage_bps / 100:.1f}%\n"
        f"Current trade fee: {fc.fee_bps / 100:.1f}% (goes to dev wallet)\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Slippage: 0.5%", callback_data="settings:slippage:50"),
            InlineKeyboardButton(text="1%", callback_data="settings:slippage:100"),
            InlineKeyboardButton(text="3%", callback_data="settings:slippage:300"),
        ],
        [InlineKeyboardButton(text="🔑 Export Seed Phrase", callback_data="settings:export_seed")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="menu:home")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("settings:slippage:"))
async def set_slippage(callback: CallbackQuery):
    bps = int(callback.data.split(":")[2])
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = result.scalar_one()
        user.slippage_bps = bps
        await session.commit()
    await callback.answer(f"Slippage set to {bps/100:.1f}%")
    await show_settings(callback)


@router.callback_query(F.data == "settings:export_seed")
async def export_seed_warning(callback: CallbackQuery, state: FSMContext):
    # Deliberately NOT a one-tap reveal — exporting the key that controls
    # real funds should have friction, so this requires typing a literal
    # confirmation word before anything is decrypted or shown.
    await state.set_state(ExportSeed.waiting_for_confirm_text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:settings")]
    ])
    await callback.message.edit_text(
        "🔑 <b>Export Seed Phrase</b>\n\n"
        "⚠️ Anyone who sees your seed phrase can take everything in this "
        "wallet. Only do this in a private chat, with no one else able to "
        "see your screen.\n\n"
        f"The phrase will be shown as a spoiler and this message will "
        f"auto-delete after {SEED_AUTO_DELETE_SECONDS} seconds.\n\n"
        "Type <b>REVEAL</b> to confirm, or tap Cancel.",
        reply_markup=kb,
    )
    await callback.answer()


@router.message(ExportSeed.waiting_for_confirm_text)
async def export_seed_reveal(message: Message, state: FSMContext, bot: Bot):
    await state.clear()

    if message.text.strip().upper() != "REVEAL":
        await message.answer("Cancelled — didn't receive REVEAL.")
        return

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if not user:
            await message.answer("No wallet found.")
            return
        wallet_result = await session.execute(
            select(Wallet).where(Wallet.user_id == user.id, Wallet.is_primary == True)  # noqa: E712
        )
        wallet = wallet_result.scalar_one_or_none()

    if not wallet:
        await message.answer("No wallet found.")
        return

    try:
        mnemonic = encryption.decrypt_mnemonic(wallet.encrypted_mnemonic, wallet.wrapped_data_key)
    except Exception:
        await message.answer(
            "❌ Couldn't decrypt your seed phrase. This usually means the server's "
            "encryption key changed since your wallet was created — contact support."
        )
        return

    sent = await message.answer(
        f"<code>{wallet.address}</code>\n\n"
        f"<tg-spoiler>{mnemonic}</tg-spoiler>\n\n"
        f"🗑️ This message self-destructs in {SEED_AUTO_DELETE_SECONDS}s. "
        f"Save it somewhere safe now, then delete it yourself too if it's still here."
    )

    async def _delete_later():
        await asyncio.sleep(SEED_AUTO_DELETE_SECONDS)
        try:
            await bot.delete_message(chat_id=sent.chat.id, message_id=sent.message_id)
        except Exception:
            pass  # message may already be deleted by the user, or bot lacks delete rights in this chat type

    asyncio.create_task(_delete_later())
