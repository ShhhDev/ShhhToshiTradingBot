import asyncio
import logging

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

import encryption
import fees as fee_service
from db import async_session, User
from helpers import (
    edit, esc, fmt_pct_bps, get_active_wallet, get_or_create_user, get_wallet_by_id,
    list_wallets, require_wallet, reply_or_edit, set_active_wallet, short_addr, spawn,
)

logger = logging.getLogger(__name__)
router = Router()

SEED_AUTO_DELETE_SECONDS = 45
SLIPPAGE_OPTIONS_BPS = [50, 100, 300, 500]  # 0.5%, 1%, 3%, 5%


def _id_from(data: str) -> int | None:
    try:
        return int(data.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


# ---- main settings screen -------------------------------------------------------

async def build_settings(tg_user) -> tuple[str, InlineKeyboardMarkup]:
    user = await get_or_create_user(tg_user)
    wallet = await get_active_wallet(tg_user.id)
    wallets = await list_wallets(tg_user.id)
    fc = await fee_service.get_fee_config()

    text = (
        "⚙️ <b>Settings</b>\n\n"
        f"👛 Active wallet:\n<code>{wallet.address}</code>\n"
        f"📚 Wallets in your account: {len(wallets)}\n"
        f"📊 Slippage tolerance: {user.slippage_bps / 100:g}%\n"
        f"💸 Trade fee: {fmt_pct_bps(fc.fee_bps)}\n\n"
        "Tap a percentage below to change your slippage."
    )
    slip_row = [
        InlineKeyboardButton(
            text=("✅ " if user.slippage_bps == bps else "") + f"{bps / 100:g}%",
            callback_data=f"set:slip:{bps}",
        )
        for bps in SLIPPAGE_OPTIONS_BPS
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👛 My Wallets", callback_data="set:wallets"),
            InlineKeyboardButton(text="➕ Add Wallet", callback_data="set:add"),
        ],
        [InlineKeyboardButton(text="🔑 View Seed Phrase", callback_data=f"set:seed:{wallet.id}")],
        slip_row,
        [InlineKeyboardButton(text="🏠 Home", callback_data="menu:home")],
    ])
    return text, kb


async def send_settings(event):
    """Entry point for the ⚙️ Settings menu button (Message) and every 'back' (CallbackQuery)."""
    if not await require_wallet(event):
        return
    if isinstance(event, CallbackQuery):
        await event.answer()
    text, kb = await build_settings(event.from_user)
    await reply_or_edit(event, text, kb)


@router.callback_query(F.data == "set:home")
async def settings_home(callback: CallbackQuery):
    await send_settings(callback)


# ---- slippage -----------------------------------------------------------------------

@router.callback_query(F.data.startswith("set:slip:"))
async def set_slippage(callback: CallbackQuery):
    bps = _id_from(callback.data)
    if bps not in SLIPPAGE_OPTIONS_BPS:
        await callback.answer()
        return
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = result.scalars().first()
        if user:
            user.slippage_bps = bps
            await session.commit()
    await callback.answer(f"Slippage set to {bps / 100:g}%")
    text, kb = await build_settings(callback.from_user)
    await edit(callback.message, text, kb)


# ---- wallets ------------------------------------------------------------------------

@router.callback_query(F.data == "set:wallets")
async def show_wallets(callback: CallbackQuery):
    await callback.answer()
    await _render_wallets(callback)


async def _render_wallets(callback: CallbackQuery):
    """Draws the wallet list. Does not answer the callback (callers do, exactly once)."""
    wallets = await list_wallets(callback.from_user.id)
    active = await get_active_wallet(callback.from_user.id)
    rows = []
    for i, w in enumerate(wallets, 1):
        mark = "✅ " if active and w.id == active.id else ""
        rows.append([InlineKeyboardButton(text=f"{mark}#{i}  {short_addr(w.address)}", callback_data=f"set:w:{w.id}")])
    rows.append([InlineKeyboardButton(text="➕ Add Wallet", callback_data="set:add")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="set:home")])
    await edit(
        callback.message,
        "👛 <b>My Wallets</b>\n\n✅ marks the wallet used for trading and deposits.\n"
        "Tap a wallet to switch to it or view its seed phrase.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("set:w:"))
async def wallet_detail(callback: CallbackQuery):
    wid = _id_from(callback.data)
    wallet = await get_wallet_by_id(callback.from_user.id, wid) if wid is not None else None
    if not wallet:
        await callback.answer("Wallet not found.", show_alert=True)
        return
    await callback.answer()

    wallets = await list_wallets(callback.from_user.id)
    active = await get_active_wallet(callback.from_user.id)
    number = next((i for i, w in enumerate(wallets, 1) if w.id == wallet.id), "?")
    is_active = bool(active and active.id == wallet.id)
    origin = "Imported" if wallet.imported else "Created in this bot"

    text = (
        f"👛 <b>Wallet #{number}</b>{' — active' if is_active else ''}\n\n"
        f"<code>{wallet.address}</code>\n\n"
        f"{origin} · {wallet.created_at:%Y-%m-%d}"
    )
    rows = []
    if not is_active:
        rows.append([InlineKeyboardButton(text="✅ Use this wallet", callback_data=f"set:use:{wallet.id}")])
    rows.append([InlineKeyboardButton(text="🔑 View seed phrase", callback_data=f"set:seed:{wallet.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="set:wallets")])
    await edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("set:use:"))
async def use_wallet(callback: CallbackQuery):
    wid = _id_from(callback.data)
    ok = await set_active_wallet(callback.from_user.id, wid) if wid is not None else False
    if not ok:
        await callback.answer("Wallet not found.", show_alert=True)
        return
    await callback.answer("✅ Active wallet switched")
    await _render_wallets(callback)


@router.callback_query(F.data == "set:add")
async def add_wallet_menu(callback: CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Create a new wallet", callback_data="wallet:create")],
        [InlineKeyboardButton(text="📥 Import with seed phrase", callback_data="wallet:import")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="set:home")],
    ])
    await edit(
        callback.message,
        "➕ <b>Add Wallet</b>\n\nCreate a brand-new wallet, or import one you already own. "
        "The wallet you add becomes your active wallet; your other wallets stay saved.",
        kb,
    )


# ---- seed phrase ---------------------------------------------------------------------

@router.callback_query(F.data.startswith("set:seed:"))
async def seed_warning(callback: CallbackQuery):
    wid = _id_from(callback.data)
    wallet = await get_wallet_by_id(callback.from_user.id, wid) if wid is not None else None
    if not wallet:
        await callback.answer("Wallet not found.", show_alert=True)
        return
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Show my seed phrase", callback_data=f"set:reveal:{wallet.id}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="set:home")],
    ])
    await edit(
        callback.message,
        "🔑 <b>View Seed Phrase</b>\n\n"
        f"Wallet: <code>{wallet.address}</code>\n\n"
        "⚠️ Anyone who sees this phrase can take everything in the wallet. Only continue "
        "in this private chat, with nobody able to see your screen.\n\n"
        f"It's shown as a hidden spoiler and the message deletes itself after {SEED_AUTO_DELETE_SECONDS} seconds.",
        kb,
    )


@router.callback_query(F.data.startswith("set:reveal:"))
async def seed_reveal(callback: CallbackQuery, bot: Bot):
    wid = _id_from(callback.data)
    wallet = await get_wallet_by_id(callback.from_user.id, wid) if wid is not None else None
    if not wallet:
        await callback.answer("Wallet not found.", show_alert=True)
        return

    try:
        mnemonic = encryption.decrypt_mnemonic(wallet.encrypted_mnemonic, wallet.wrapped_data_key)
    except Exception:
        logger.exception(f"seed decrypt failed for wallet {wallet.id}")
        await callback.answer(
            "Couldn't decrypt this wallet's seed phrase (the server's encryption key may have changed).",
            show_alert=True,
        )
        return

    await callback.answer()
    logger.info(f"seed phrase viewed: user={callback.from_user.id} wallet={wallet.id}")  # never log the phrase itself

    sent = await callback.message.answer(
        f"🔑 <b>Seed phrase</b>\n<code>{wallet.address}</code>\n\n"
        f"<tg-spoiler>{esc(mnemonic)}</tg-spoiler>\n\n"
        f"🗑 Deletes itself in {SEED_AUTO_DELETE_SECONDS}s. Save it somewhere safe and offline.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Delete now", callback_data="set:del")]
        ]),
    )
    await edit(callback.message, "✅ Your seed phrase was sent below. It will delete itself shortly.")
    spawn(_delete_later(bot, sent.chat.id, sent.message_id, SEED_AUTO_DELETE_SECONDS))


@router.callback_query(F.data == "set:del")
async def delete_message_now(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Deleted")


async def _delete_later(bot: Bot, chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # already deleted by the user
