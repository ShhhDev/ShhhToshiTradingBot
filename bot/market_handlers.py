"""📍 Positions (holdings with values) and 🔍 Explore coins (trending)."""

import addr_utils
import ton_client
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from helpers import esc, fmt_amount, fmt_usd, reply_or_edit, require_wallet

router = Router()


async def send_positions(event):
    wallet = await require_wallet(event)
    if not wallet:
        return
    if isinstance(event, CallbackQuery):
        await event.answer()
    try:
        ton_bal = await ton_client.get_ton_balance(wallet.address)
        holds = await ton_client.get_jetton_holdings(wallet.address)
        prices = await ton_client.get_usd_prices([h["contract"] for h in holds])
    except Exception:
        await reply_or_edit(event, "⚠️ Couldn't load your positions. Please try again in a few seconds.")
        return

    ton_usd = prices.get("TON")
    rows = []
    for h in holds:
        p = prices.get(h["raw"])
        rows.append((h, p, h["balance"] * p if p else None))
    rows.sort(key=lambda r: r[2] or 0, reverse=True)
    total = (ton_bal * ton_usd if ton_usd else 0) + sum(r[2] or 0 for r in rows)

    lines = ["📍 <b>Positions</b>", f"Total ≈ <b>{fmt_usd(total)}</b>\n",
             f"💎 TON — {fmt_amount(ton_bal)}" + (f" ({fmt_usd(ton_bal * ton_usd)})" if ton_usd else "")]
    for h, p, val in rows[:25]:
        lines.append(f"🪙 {esc(h['symbol'])} — {fmt_amount(h['balance'])}" + (f" ({fmt_usd(val)})" if val else " (no price)"))
    if not rows:
        lines.append("\nNo tokens yet. Paste a contract address to buy one.")

    buttons = [InlineKeyboardButton(text=f"🪙 {h['symbol'][:12]}", callback_data=f"tk:{h['contract']}") for h, _, _ in rows[:20]]
    kb = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    kb.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="pos:refresh"), InlineKeyboardButton(text="🏠 Home", callback_data="menu:home")])
    await reply_or_edit(event, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data == "pos:refresh")
async def refresh_positions(callback: CallbackQuery):
    await send_positions(callback)


async def send_explore(event):
    if isinstance(event, CallbackQuery):
        await event.answer()
    coins = await ton_client.get_trending(10)
    if not coins:
        await reply_or_edit(event, "⚠️ Couldn't load trending coins right now. Try again shortly, "
                                   "or paste a contract address.")
        return
    lines = ["🔍 <b>Trending on TON</b>\n"]
    for i, c in enumerate(coins, 1):
        ch = c["change_24h"]
        ch_txt = f"{float(ch):+.1f}%" if ch not in (None, "") else "—"
        lines.append(f"{i}. <b>{esc(c['symbol'])}</b> · {fmt_usd(c['price_usd'])} · 24h {ch_txt}")
    buttons = [InlineKeyboardButton(text=c["symbol"][:12], callback_data=f"tk:{c['contract']}") for c in coins]
    kb = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    kb.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="exp:refresh"), InlineKeyboardButton(text="🏠 Home", callback_data="menu:home")])
    await reply_or_edit(event, "\n".join(lines) + "\n\nTap a coin to open it.", InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data == "exp:refresh")
async def refresh_explore(callback: CallbackQuery):
    await send_explore(callback)
