from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from db import async_session, User, Wallet
import ton_client

router = Router()


async def _get_primary_wallet(telegram_id: int) -> Wallet | None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            return None
        wallet_result = await session.execute(
            select(Wallet).where(Wallet.user_id == user.id, Wallet.is_primary == True)  # noqa: E712
        )
        return wallet_result.scalar_one_or_none()


@router.callback_query(F.data == "menu:balance")
async def show_balance(callback: CallbackQuery):
    wallet = await _get_primary_wallet(callback.from_user.id)
    if not wallet:
        await callback.answer("No wallet found. Create one first.", show_alert=True)
        return

    await callback.answer("Fetching balances…")

    ton_balance = await ton_client.get_ton_balance(wallet.address)
    holdings = await ton_client.get_jetton_holdings(wallet.address)

    # NOTE: USD/TON pricing for "total balance" needs a price feed
    # (e.g. STON.fi pool prices or a CoinGecko-style aggregator) — wire in
    # services/pricing.py. Left as TON-denominated only for now.

    lines = [
        "💰 <b>Your Balance</b>\n",
        f"<b>Wallet:</b> <code>{wallet.address}</code>\n",
        f"<b>TON:</b> {ton_balance:.4f} TON",
    ]

    if holdings:
        lines.append("\n<b>Token Holdings:</b>")
        for h in holdings:
            lines.append(f"• {h['symbol']} — {h['balance']:.4f}  <i>(tap below for price/MC/liquidity)</i>")
    else:
        lines.append("\n<i>No token holdings yet.</i>")

    lines.append(
        "\n\n<i>Note: this shows your on-chain TON wallet balance. Telegram "
        "itself does not have a separate wallet balance beyond TON assets "
        "held by this address — \"Telegram balance\" here refers to this "
        "same on-chain wallet, shown for clarity in one place.</i>"
    )

    kb_rows = []
    for h in holdings[:10]:  # inline buttons per holding to trade directly
        kb_rows.append([
            InlineKeyboardButton(text=f"Sell {h['symbol']}", callback_data=f"sell:holding:{h['contract']}"),
            InlineKeyboardButton(text=f"Buy more {h['symbol']}", callback_data=f"buy:holding:{h['contract']}"),
        ])
    kb_rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="menu:balance")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="menu:home")])

    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
