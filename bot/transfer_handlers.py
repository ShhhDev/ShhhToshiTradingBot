"""
Transfer (withdraw) TON or any jetton to another address. Free of bot fees:
fees apply to trades only, never to deposits or withdrawals. Network gas is paid by the wallet.
"""

import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import addr_utils
import encryption
import ton_client
from helpers import edit, esc, fmt_amount, get_active_wallet, reply_or_edit, require_wallet

logger = logging.getLogger(__name__)
router = Router()

TON = "TON"
TON_RESERVE = 0.03       # left in the wallet for the transfer's network fee
JETTON_GAS_MIN = 0.1     # TON needed in the wallet to move a jetton


class TransferFlow(StatesGroup):
    amount = State()
    dest = State()
    memo = State()
    confirm = State()


def _cancel() -> list:
    return [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:home")]


async def start_transfer(event, state: FSMContext):
    wallet = await require_wallet(event)
    if not wallet:
        return
    await state.clear()
    if isinstance(event, CallbackQuery):
        await event.answer()
    try:
        holdings = await ton_client.get_jetton_holdings(wallet.address)
    except Exception:
        await reply_or_edit(event, "⚠️ Couldn't load your balances. Please try again in a few seconds.")
        return
    buttons = [InlineKeyboardButton(text="💎 TON", callback_data=f"tr:t:{TON}")]
    buttons += [InlineKeyboardButton(text=f"🪙 {h['symbol'][:14]}", callback_data=f"tr:t:{h['contract']}")
                for h in holdings[:20]]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)] + [_cancel()]
    await reply_or_edit(
        event,
        "↗️ <b>Transfer</b>\n\nChoose what to send. Transfers have <b>no bot fee</b> — only the network fee.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("tr:t:"))
async def pick_token(callback: CallbackQuery, state: FSMContext):
    token = callback.data[5:]
    if token != TON and not addr_utils.is_valid(token):
        await callback.answer()
        return
    wallet = await require_wallet(callback)
    if not wallet:
        return
    await callback.answer()
    try:
        ton_bal = await ton_client.get_ton_balance(wallet.address)
        if token == TON:
            sym, dec, bal = "TON", 9, ton_bal
            usable = max(ton_bal - TON_RESERVE, 0.0)
        else:
            meta = await ton_client.get_token_metadata(token)
            if meta is None:
                await edit(callback.message, "❌ Couldn't load that token.")
                return
            sym, dec = meta["symbol"], meta["decimals"]
            held = [h for h in await ton_client.get_jetton_holdings(wallet.address)
                    if addr_utils.same(h["contract"], token)]
            bal = held[0]["balance"] if held else 0.0
            usable = bal
            if bal > 0 and ton_bal < JETTON_GAS_MIN:
                await edit(callback.message, f"❌ You need at least {JETTON_GAS_MIN:g} TON in the wallet for network fees.")
                return
    except Exception:
        await edit(callback.message, "⚠️ Couldn't load your balances. Please try again in a few seconds.")
        return
    if usable <= 0:
        await edit(callback.message, f"❌ Nothing to send: you have {fmt_amount(bal)} {esc(sym)}.")
        return

    await state.set_state(TransferFlow.amount)
    await state.update_data(token=token, sym=sym, dec=min(dec, 9), usable=usable)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("Max" if p == 100 else f"{p}%"), callback_data=f"tr:p:{p}") for p in (25, 50, 75, 100)],
        _cancel(),
    ])
    await edit(callback.message,
               f"↗️ <b>Send {esc(sym)}</b>\n\nAvailable: <b>{fmt_amount(usable, 6)} {esc(sym)}</b>\n\n"
               "Type an amount or tap a preset:", kb)


async def _got_amount(event, state: FSMContext, amount: float, preset: bool):
    data = await state.get_data()
    usable, dec, sym = float(data.get("usable", 0)), int(data.get("dec", 9)), esc(data.get("sym", ""))
    scale = 10 ** dec
    if preset:
        amount = int(min(amount, usable) * scale) / scale
    if not (amount == amount) or amount <= 0 or amount > usable + 1e-9:
        await reply_or_edit(event, f"❌ Enter an amount between 0 and {fmt_amount(usable, 6)} {sym}.")
        return
    await state.update_data(amount=amount)
    await state.set_state(TransferFlow.dest)
    await reply_or_edit(event, f"Sending <b>{fmt_amount(amount, 6)} {sym}</b>.\n\nPaste the <b>destination address</b>:",
                        InlineKeyboardMarkup(inline_keyboard=[_cancel()]))


@router.message(TransferFlow.amount)
async def amount_typed(message: Message, state: FSMContext):
    raw = (message.text or "").strip().lower().replace(",", ".")
    try:
        usable = float((await state.get_data()).get("usable", 0))
        amount = usable if raw in ("max", "all") else float(raw)
    except ValueError:
        await message.answer("Please send a number like <code>2.5</code>, or tap a preset.")
        return
    await _got_amount(message, state, amount, preset=raw in ("max", "all"))


@router.callback_query(F.data.startswith("tr:p:"), TransferFlow.amount)
async def amount_preset(callback: CallbackQuery, state: FSMContext):
    try:
        pct = int(callback.data[5:])
    except ValueError:
        await callback.answer()
        return
    if pct not in (25, 50, 75, 100):
        await callback.answer()
        return
    await callback.answer()
    usable = float((await state.get_data()).get("usable", 0))
    await _got_amount(callback, state, usable * pct / 100, preset=True)


@router.message(TransferFlow.dest)
async def dest_typed(message: Message, state: FSMContext):
    dest = (message.text or "").strip()
    wallet = await get_active_wallet(message.from_user.id)
    if not addr_utils.is_valid(dest):
        await message.answer("❌ That isn't a valid TON address. Paste it again, or /cancel.")
        return
    if wallet and addr_utils.same(dest, wallet.address):
        await message.answer("❌ That's your own wallet. Paste a different address, or /cancel.")
        return
    await state.update_data(dest=addr_utils.canonical(dest))
    await state.set_state(TransferFlow.memo)
    await message.answer(
        "📝 Add a memo/comment? (exchanges often require one). Type it, or tap Skip.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip", callback_data="tr:skip")], _cancel()]),
    )


async def _review(event, state: FSMContext, memo: str | None):
    await state.update_data(memo=memo)
    d = await state.get_data()
    await state.set_state(TransferFlow.confirm)
    text = (
        "🧾 <b>Review transfer</b>\n\n"
        f"Send: <b>{fmt_amount(d['amount'], 6)} {esc(d['sym'])}</b>\n"
        f"To: <code>{d['dest']}</code>\n"
        f"Memo: {esc(memo) if memo else '—'}\n"
        "Bot fee: none · network fee paid from your wallet\n\n"
        "⚠️ Transfers can't be undone — double-check the address."
    )
    await reply_or_edit(event, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm", callback_data="tr:go")], _cancel()]))


@router.message(TransferFlow.memo)
async def memo_typed(message: Message, state: FSMContext):
    await _review(message, state, (message.text or "").strip()[:120] or None)


@router.callback_query(F.data == "tr:skip", TransferFlow.memo)
async def memo_skip(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _review(callback, state, None)


@router.callback_query(F.data == "tr:go", TransferFlow.confirm)
async def confirm(callback: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.clear()  # a second tap can't send twice
    await callback.answer()
    wallet = await get_active_wallet(callback.from_user.id)
    if not (wallet and d.get("token") and d.get("dest") and d.get("amount")):
        await edit(callback.message, "That transfer expired — start again from 🔼 Transfer.")
        return
    await edit(callback.message, "⏳ Sending…")
    try:
        mnemonic = encryption.decrypt_mnemonic(wallet.encrypted_mnemonic, wallet.wrapped_data_key)
        if d["token"] == TON:
            tx = await ton_client.send_ton(mnemonic, d["dest"], d["amount"], d.get("memo"))
        else:
            tx = await ton_client.send_jetton(mnemonic, wallet.address, d["token"], d["dest"], d["amount"], d.get("memo"))
    except Exception as e:
        logger.error("transfer failed", exc_info=e)
        await edit(callback.message, "❌ The transfer couldn't be sent. Check your balance and try again. "
                                     "If you're unsure whether it went through, check your wallet history first.")
        return
    await edit(callback.message,
               f"✅ <b>Sent</b> {fmt_amount(d['amount'], 6)} {esc(d['sym'])}\n"
               f"To: <code>{d['dest']}</code>\nRef: <code>{esc(tx or '—')}</code>")


@router.callback_query(F.data.startswith("tr:"))
async def stale(callback: CallbackQuery):
    await callback.answer("This screen expired — open Transfer again.", show_alert=True)
