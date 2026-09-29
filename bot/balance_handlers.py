import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import ton_client
from helpers import (
    edit, esc, fmt_amount, fmt_usd, is_admin, require_wallet,
)

logger = logging.getLogger(__name__)
router = Router()

MAX_TOKENS_SHOWN = 15


async def build_balance(wallet) -> tuple[str, InlineKeyboardMarkup]:
    """Fetches live data and renders the Balance screen. Raises on provider failure."""
    ton_balance = await ton_client.get_ton_balance(wallet.address)
    holdings = await ton_client.get_jetton_holdings(wallet.address)
    prices = await ton_client.get_usd_prices([h["contract"] for h in holdings])

    ton_price = prices.get("TON")
    ton_value = ton_balance * ton_price if ton_price else None
    for h in holdings:
        price = prices.get(h["raw"])
        h["usd"] = h["balance"] * price if price else None
    holdings.sort(key=lambda h: (h["usd"] is None, -(h["usd"] or 0), h["symbol"]))

    lines = [
        "💰 <b>Balance</b>",
        f"👛 <code>{wallet.address}</code>",
        "",
    ]
    ton_line = f"💎 <b>TON</b>: {fmt_amount(ton_balance)}"
    if ton_value is not None:
        ton_line += f"  (≈ {fmt_usd(ton_value)})"
    lines.append(ton_line)

    if holdings:
        lines += ["", f"🪙 <b>Tokens ({len(holdings)})</b>"]
        for h in holdings[:MAX_TOKENS_SHOWN]:
            flag = " 🚫" if h["verification"] == "blacklist" else ""
            line = f"• <b>{esc(h['symbol'])}</b>{flag} — {fmt_amount(h['balance'])}"
            if h["usd"] is not None:
                line += f"  (≈ {fmt_usd(h['usd'])})"
            lines.append(line)
        if len(holdings) > MAX_TOKENS_SHOWN:
            lines.append(f"…and {len(holdings) - MAX_TOKENS_SHOWN} more")
        lines += ["", "Tap a token below to buy or sell it."]
    else:
        lines += ["", "<i>No tokens yet — deposit some, or paste a token address to buy one.</i>"]

    values = [v for v in [ton_value] + [h["usd"] for h in holdings] if v is not None]
    if values:
        unpriced = sum(1 for h in holdings if h["usd"] is None)
        total = f"\n📊 <b>Total:</b> ≈ {fmt_usd(sum(values))}"
        if unpriced:
            total += f"  <i>(+{unpriced} token{'s' if unpriced != 1 else ''} without a price)</i>"
        lines.append(total)

    rows = []
    shown = holdings[:MAX_TOKENS_SHOWN]
    for i in range(0, len(shown), 2):
        rows.append([
            InlineKeyboardButton(text=f"🪙 {h['symbol'][:14]}", callback_data=f"tk:{h['contract']}")
            for h in shown[i:i + 2]
        ])
    rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="bal:refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def send_balance(event):
    """Entry point for both the 💰 Balance menu button (Message) and Refresh (CallbackQuery)."""
    wallet = await require_wallet(event)
    if not wallet:
        return

    loading = None
    if isinstance(event, CallbackQuery):
        await event.answer("🔄 Refreshing…")
    else:
        loading = await event.answer("⏳ Fetching your balance…")

    try:
        text, kb = await build_balance(wallet)
    except Exception as e:
        logger.exception("balance lookup failed")
        text = (
            "⚠️ <b>Couldn't load your balance right now.</b>\n\n"
            "The blockchain data provider didn't respond. Please try again in a few seconds."
        )
        if is_admin(event.from_user.id):
            text += f"\n\n<i>Admin detail:</i> <code>{esc(str(e) or type(e).__name__)[:300]}</code>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Try again", callback_data="bal:refresh")]
        ])

    await edit(loading if loading else event.message, text, kb)


@router.callback_query(F.data == "bal:refresh")
async def refresh_balance(callback: CallbackQuery):
    await send_balance(callback)
