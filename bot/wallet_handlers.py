import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

import dashboard
import encryption
import ton_client
from db import async_session, Wallet
from helpers import edit, esc, get_or_create_user, main_reply_kb

logger = logging.getLogger(__name__)
router = Router()

READY_TEXT = "✅ You're all set — here's your dashboard."


class ImportWallet(StatesGroup):
    waiting_for_mnemonic = State()


class ConfirmNewWallet(StatesGroup):
    waiting_for_ack = State()


# ---- create ------------------------------------------------------------------

@router.callback_query(F.data == "wallet:create")
async def create_wallet_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    mnemonic, address = ton_client.create_new_wallet()
    await state.set_state(ConfirmNewWallet.waiting_for_ack)
    await state.update_data(pending_mnemonic=mnemonic, pending_address=address)

    text = (
        "🆕 <b>Your new wallet</b>\n\n"
        f"<code>{address}</code>\n\n"
        "⚠️ <b>Write down your seed phrase below and store it somewhere safe "
        "OFFLINE.</b> Anyone with these words can take everything in this wallet.\n\n"
        f"<tg-spoiler>{esc(mnemonic)}</tg-spoiler>\n\n"
        "Tap below once you've saved it. This message is replaced as soon as you "
        "confirm. You can view the phrase again later from ⚙️ Settings."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ I've saved my seed phrase", callback_data="wallet:confirm_create")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:home")],
    ])
    await edit(callback.message, text, kb)


@router.callback_query(F.data == "wallet:confirm_create", ConfirmNewWallet.waiting_for_ack)
async def create_wallet_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mnemonic, address = data.get("pending_mnemonic"), data.get("pending_address")
    await state.clear()
    await callback.answer()
    if not mnemonic or not address:
        await edit(callback.message, "⚠️ That request expired. Tap ⚙️ Settings → Add Wallet to start again.")
        return

    await _persist_wallet(callback.from_user, mnemonic, address, imported=False)
    await edit(callback.message, f"✅ <b>Wallet created and secured.</b>\n\n<code>{address}</code>")
    await callback.message.answer(READY_TEXT, reply_markup=main_reply_kb())
    await dashboard.send_dashboard(callback)


@router.callback_query(F.data == "wallet:confirm_create")
async def create_wallet_confirm_stale(callback: CallbackQuery):
    await callback.answer("This request expired - please start again.", show_alert=True)


# ---- import ------------------------------------------------------------------

@router.callback_query(F.data == "wallet:import")
async def import_wallet_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ImportWallet.waiting_for_mnemonic)
    text = (
        "📥 <b>Import Wallet</b>\n\n"
        "Send your seed phrase (12 or 24 words) as a message.\n\n"
        "⚠️ Only do this in this private chat. Your message is deleted automatically "
        "and the phrase is encrypted before it is stored.\n\n"
        "Send /cancel to abort."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:home")]
    ])
    await edit(callback.message, text, kb)


@router.message(ImportWallet.waiting_for_mnemonic)
async def import_wallet_receive(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Please send your seed phrase as text, or /cancel.")
        return

    phrase = " ".join(message.text.lower().split())

    # Remove the user's message containing the phrase right away (bots are allowed
    # to delete incoming messages in private chats).
    try:
        await message.delete()
    except Exception as e:
        logger.info(f"could not delete seed phrase message: {e}")

    try:
        address = ton_client.import_wallet_from_mnemonic(phrase)
    except ValueError as e:
        await message.answer(f"❌ {esc(e)}\n\nSend it again, or /cancel.")
        return

    status = await _persist_wallet(message.from_user, phrase, address, imported=True)
    await state.clear()

    if status == "duplicate_other":
        await message.answer("❌ That wallet is already registered with another account in this bot.")
    elif status == "duplicate_self":
        await message.answer(
            f"ℹ️ That wallet is already in your account — it's now your active wallet.\n\n<code>{address}</code>",
            reply_markup=main_reply_kb(),
        )
        await dashboard.send_dashboard(message)
    else:
        await message.answer(
            f"✅ <b>Wallet imported and secured.</b>\n\n<code>{address}</code>\n\n"
            "Your message with the phrase was deleted from this chat.",
            reply_markup=main_reply_kb(),
        )
        await dashboard.send_dashboard(message)


# ---- cancel ------------------------------------------------------------------

@router.message(F.text == "/cancel")
async def cancel_flow(message: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Cancelled.")


# ---- storage -----------------------------------------------------------------

async def _persist_wallet(tg_user, mnemonic: str, address: str, imported: bool) -> str:
    """Stores a wallet and makes it the user's active one.
    Returns "ok", "duplicate_self" (already theirs; just re-activated) or "duplicate_other"."""
    user = await get_or_create_user(tg_user)
    encrypted_mnemonic, wrapped_key = encryption.encrypt_mnemonic(mnemonic)

    async with async_session() as session:
        existing = (await session.execute(
            select(Wallet).where(Wallet.address == address)
        )).scalars().first()

        own = (await session.execute(
            select(Wallet).where(Wallet.user_id == user.id)
        )).scalars().all()

        if existing is not None:
            if existing.user_id != user.id:
                return "duplicate_other"
            for w in own:
                w.is_primary = (w.id == existing.id)
            await session.commit()
            return "duplicate_self"

        for w in own:
            w.is_primary = False
        session.add(Wallet(
            user_id=user.id,
            address=address,
            encrypted_mnemonic=encrypted_mnemonic,
            wrapped_data_key=wrapped_key,
            is_primary=True,
            imported=imported,
        ))
        await session.commit()
    return "ok"
