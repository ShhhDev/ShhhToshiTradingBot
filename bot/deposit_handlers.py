import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import deposit_watcher
import ton_client
from db import async_session
from helpers import edit, esc, fmt_amount, is_admin, require_wallet

logger = logging.getLogger(__name__)
router = Router()


def _deposit_screen_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Check Deposit", callback_data="dep:check")],
    ])


def _deposit_text(address: str) -> str:
    return (
        "📥 <b>Deposit</b>\n\n"
        "Send <b>TON</b> or any <b>jetton on the TON network</b> to your wallet address:\n\n"
        f"<code>{address}</code>\n"
        "<i>(tap the address to copy it)</i>\n\n"
        "⚠️ Only send assets on the <b>TON blockchain</b>. Anything sent on another "
        "network to this address is lost permanently.\n\n"
        "After sending, tap <b>Check Deposit</b>. You'll also get a message here "
        "automatically as soon as a deposit is detected."
    )


async def send_deposit(event):
    wallet = await require_wallet(event)
    if not wallet:
        return
    await event.answer(_deposit_text(wallet.address), reply_markup=_deposit_screen_kb())




@router.callback_query(F.data == "dep:home")
async def deposit_home(callback: CallbackQuery):
    wallet = await require_wallet(callback)
    if not wallet:
        return
    await callback.answer()
    await edit(callback.message, _deposit_text(wallet.address), _deposit_screen_kb())


@router.callback_query(F.data == "dep:check")
async def check_deposit(callback: CallbackQuery):
    wallet = await require_wallet(callback)
    if not wallet:
        return
    await callback.answer("🔎 Checking…")

    try:
        async with async_session() as session:
            found = await deposit_watcher.detect_new_deposits(session, wallet, strict=True)
        ton_balance = await ton_client.get_ton_balance(wallet.address)
    except Exception as e:
        logger.exception("manual deposit check failed")
        text = (
            "⚠️ <b>Couldn't check right now.</b>\n\n"
            "The blockchain data provider didn't respond. Please try again in a few seconds."
        )
        if is_admin(callback.from_user.id):
            text += f"\n\n<i>Admin detail:</i> <code>{esc(str(e) or type(e).__name__)[:300]}</code>"
        await edit(callback.message, text, _deposit_screen_kb())
        return

    if found:
        lines = ["🎉 <b>Deposit received!</b>", ""]
        for dep in found:
            lines.append(f"+{fmt_amount(dep['amount'], 6)} <b>{esc(dep['symbol'])}</b>")
        lines += ["", f"💎 TON balance: {fmt_amount(ton_balance)}"]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 View Balance", callback_data="bal:refresh")],
            [InlineKeyboardButton(text="🔄 Check again", callback_data="dep:check")],
        ])
        await edit(callback.message, "\n".join(lines), kb)
    else:
        text = (
            "⏳ <b>No new deposit yet.</b>\n\n"
            f"💎 TON balance: {fmt_amount(ton_balance)}\n\n"
            "Transfers usually show up within 10–30 seconds. Tap <b>Check Deposit</b> again in a moment.\n\n"
            f"Your address:\n<code>{wallet.address}</code>\n\n"
            f"<i>Last checked {datetime.utcnow().strftime('%H:%M:%S')} UTC</i>"
        )
        await edit(callback.message, text, _deposit_screen_kb())
