from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from db import async_session, User, Wallet
import ton_client
import encryption
from start_handlers import main_menu_kb

router = Router()


class ImportWallet(StatesGroup):
    waiting_for_mnemonic = State()


class ConfirmNewWallet(StatesGroup):
    waiting_for_ack = State()


@router.callback_query(F.data == "wallet:create")
async def create_wallet_start(callback: CallbackQuery, state: FSMContext):
    mnemonic, address = ton_client.create_new_wallet()
    await state.update_data(pending_mnemonic=mnemonic, pending_address=address)
    await state.set_state(ConfirmNewWallet.waiting_for_ack)

    text = (
        "🆕 <b>Your new wallet</b>\n\n"
        f"<code>{address}</code>\n\n"
        "⚠️ <b>Write down your seed phrase below and store it somewhere safe "
        "OFFLINE.</b> Anyone with these words can take everything in this wallet. "
        "Delete this message after saving it.\n\n"
        f"<tg-spoiler>{mnemonic}</tg-spoiler>\n\n"
        "Tap below once you've saved it. Your phrase will also be encrypted "
        "and stored securely so the bot can trade for you — it is never shown "
        "again after this message."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ I've saved my seed phrase", callback_data="wallet:confirm_create")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:home")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "wallet:confirm_create", ConfirmNewWallet.waiting_for_ack)
async def create_wallet_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mnemonic, address = data["pending_mnemonic"], data["pending_address"]
    await _persist_wallet(callback.from_user, mnemonic, address, imported=False)
    await state.clear()

    await callback.message.edit_text(
        f"✅ Wallet created and secured.\n\n<code>{address}</code>",
        reply_markup=main_menu_kb(has_wallet=True),
    )
    await callback.answer()


@router.callback_query(F.data == "wallet:import")
async def import_wallet_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ImportWallet.waiting_for_mnemonic)
    text = (
        "📥 <b>Import Wallet</b>\n\n"
        "Send your 12 or 24-word seed phrase as a message.\n\n"
        "⚠️ Only do this in a private chat you trust. Your phrase will be "
        "encrypted immediately and stored securely — delete your message "
        "with the phrase afterward for safety.\n\n"
        "Send /cancel to abort."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:home")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.message(ImportWallet.waiting_for_mnemonic)
async def import_wallet_receive(message: Message, state: FSMContext):
    try:
        address = ton_client.import_wallet_from_mnemonic(message.text)
    except ValueError as e:
        await message.answer(f"❌ {e}\n\nTry again, or send /cancel.")
        return

    await _persist_wallet(message.from_user, message.text.strip(), address, imported=True)
    await state.clear()

    # best-effort: remind user to delete their message (bot can't delete user msgs
    # without delete permission in the chat, so just instruct them)
    await message.answer(
        f"✅ Wallet imported and secured.\n\n<code>{address}</code>\n\n"
        "🗑️ Please delete your previous message containing the seed phrase now.",
        reply_markup=main_menu_kb(has_wallet=True),
    )


@router.message(F.text == "/cancel")
async def cancel_flow(message: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Cancelled.")


async def _persist_wallet(tg_user, mnemonic: str, address: str, imported: bool):
    encrypted_mnemonic, wrapped_key = encryption.encrypt_mnemonic(mnemonic)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_user.id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=tg_user.id, username=tg_user.username)
            session.add(user)
            await session.commit()
            await session.refresh(user)

        wallet = Wallet(
            user_id=user.id,
            address=address,
            encrypted_mnemonic=encrypted_mnemonic,
            wrapped_data_key=wrapped_key,
            is_primary=True,
            imported=imported,
        )
        session.add(wallet)
        await session.commit()
