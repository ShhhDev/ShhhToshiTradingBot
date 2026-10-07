"""
The main dashboard: one inline-button screen that reaches every feature.
New user: /start -> create or import a wallet -> this dashboard.
Every Back / Cancel button in the bot returns here (callback "menu:home").
"""

import asyncio

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import addr_utils
import auto_handlers
import fees
import referral_handlers
import ton_client
import trade_handlers
import transfer_handlers
from helpers import (
    edit, esc, fmt_amount, fmt_pct_bps, fmt_usd, get_active_wallet, is_admin, onboarding_kb, require_wallet,
)

router = Router()


class DashFlow(StatesGroup):
    buy_ca = State()

WELCOME_TEXT = (
    "👋 <b>Welcome to ShhhToshi</b>\n\n"
    "Trade TON tokens directly from Telegram — swap, snipe, copy-trade and set limit orders.\n\n"
    "🔐 Your seed phrase is <b>encrypted and stored securely</b> so the bot can trade on your "
    "behalf. Never share it with anyone, and only import wallets you're comfortable trading "
    "through a bot.\n\n"
    "To get started, create a new wallet or import one:"
)


def _b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _safe(coro, default=None):
    try:
        return await coro
    except Exception:
        return default


async def build_dashboard(tg_user) -> tuple[str, InlineKeyboardMarkup]:
    wallet = await get_active_wallet(tg_user.id)
    balance, prices, fc = await asyncio.gather(
        _safe(ton_client.get_ton_balance(wallet.address)),
        _safe(ton_client.get_usd_prices([]), {}),
        _safe(fees.get_fee_config()),
    )
    ton_usd = (prices or {}).get("TON")

    text = "🚀 <b>ShhhToshi</b> — trade on TON faster than anyone else\n\n📊 <b>Market</b>\n"
    text += f"💎 TON price: <b>{fmt_usd(ton_usd)}</b>\n\n" if ton_usd else "💎 TON price: —\n\n"
    text += f"👛 <b>Wallet</b> <code>{wallet.address}</code>\n"
    if balance is not None:
        text += f"Balance: <b>{fmt_amount(balance)} TON</b>" + (f" (≈ {fmt_usd(balance * ton_usd)})" if ton_usd else "") + "\n"
    if fc is not None:
        text += f"\n💸 Trade fee: {fmt_pct_bps(fc.fee_bps)} · deposits & withdrawals are free"
    text += "\n\n<i>Tip: paste any token contract address to open it and trade.</i>"

    wallets_label = f"Wallets 👛 [{fmt_amount(balance)} 💎]" if balance is not None else "Wallets 👛"
    rows = [
        [_b(wallets_label, "set:wallets")],
        [_b("🟢 Buy", "dash:buy"), _b("🔴 Sell", "dash:sell")],
        [_b("↗️ Transfer", "dash:transfer"), _b("📍 Positions", "pos:refresh")],
        [_b("🔍 Explore coins", "exp:refresh")],
        [_b("🔁 Swap", "swap:start"), _b("💰 Balance", "bal:refresh")],
        [_b("📸 Copy Trade", "dash:copy"), _b("🎯 Snipes", "dash:snipes")],
        [_b("🗒 Limit orders", "dash:limit"), _b("🤝 Referral", "dash:ref")],
        [_b("📥 Deposit", "dep:open"), _b("🆘 Help", "dash:help")],
        [_b("⚙️ Settings", "set:home"), _b("📚 Usage guide", "dash:guide")],
        [_b("🔄 Refresh", "dash:refresh")],
    ]
    if is_admin(tg_user.id):
        rows.append([_b("🛠 Admin", "admin:home")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def send_dashboard(event, replace: bool = False):
    """Shows the dashboard (or the create/import screen if there's no wallet yet).
    replace=True edits the tapped message in place; otherwise it posts a new message."""
    wallet = await get_active_wallet(event.from_user.id)
    if wallet:
        text, kb = await build_dashboard(event.from_user)
    else:
        text, kb = WELCOME_TEXT, onboarding_kb()

    if isinstance(event, CallbackQuery):
        if replace:
            await edit(event.message, text, kb)
        else:
            await event.message.answer(text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)


# ---- callbacks ---------------------------------------------------------------------

@router.callback_query(F.data == "dash:refresh")
async def refresh(callback: CallbackQuery):
    await callback.answer("🔄")
    await send_dashboard(callback, replace=True)


@router.callback_query(F.data == "dash:transfer")
async def to_transfer(callback: CallbackQuery, state: FSMContext):
    await transfer_handlers.start_transfer(callback, state)


@router.callback_query(F.data == "dash:copy")
async def to_copy(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await auto_handlers.send_copy_trades(callback)


@router.callback_query(F.data == "dash:snipes")
async def to_snipes(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await auto_handlers.send_snipes(callback)


@router.callback_query(F.data == "dash:limit")
async def to_limit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await auto_handlers.send_limit_orders(callback)


@router.callback_query(F.data == "dash:ref")
async def to_referral(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await referral_handlers.send_referral(callback)


def _home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_b("🏠 Home", "menu:home")]])


@router.callback_query(F.data == "dash:help")
async def help_screen(callback: CallbackQuery):
    await callback.answer()
    fc = await _safe(fees.get_fee_config())
    fee = fmt_pct_bps(fc.fee_bps) if fc else "a small"
    await edit(callback.message, (
        "🆘 <b>Help</b>\n\n"
        "• <b>Wallets</b> — create, import, switch or back up wallets.\n"
        "• <b>Deposit</b> — send TON or jettons to your wallet address; you'll be notified.\n"
        "• <b>Swap / paste a token address</b> — buy, sell or swap any token.\n"
        "• <b>Limit orders</b> — buy when the price drops, sell when it rises.\n"
        "• <b>Copy Trade</b> — mirror another wallet's buys and sells.\n"
        "• <b>Snipes</b> — buy a token the moment it gets liquidity, or when a deployer launches one.\n"
        "• <b>Transfer</b> — withdraw TON or tokens to any address.\n\n"
        f"💸 A <b>{fee}</b> fee applies to trades. Deposits and withdrawals are free; network gas "
        "is paid from your wallet.\n\n"
        "Trading on-chain is risky — never trade more than you can afford to lose."
    ), _home_kb())


@router.callback_query(F.data == "dash:guide")
async def guide_screen(callback: CallbackQuery):
    await callback.answer()
    await edit(callback.message, (
        "📚 <b>Usage guide</b>\n\n"
        "1️⃣ <b>Fund your wallet</b> — tap Deposit and send TON. Keep ~0.5 TON for network fees.\n"
        "2️⃣ <b>Open a token</b> — paste its contract address, or use Explore coins.\n"
        "3️⃣ <b>Buy / Sell</b> — pick an amount or % and confirm on the review screen.\n"
        "4️⃣ <b>Automate</b> — Limit orders, Copy Trade and Snipes run in the background; "
        "you'll get a message for every fill.\n"
        "5️⃣ <b>Safety</b> — Settings lets you change slippage and back up your seed phrase. "
        "Never share it."
    ), _home_kb())


# ---- 🟢 Buy: paste any token's contract address ----------------------------------------

@router.callback_query(F.data == "dash:buy")
async def buy_prompt(callback: CallbackQuery, state: FSMContext):
    if not await require_wallet(callback):
        return
    await callback.answer()
    await state.clear()
    await state.set_state(DashFlow.buy_ca)
    await edit(callback.message, (
        "🟢 <b>Buy</b>\n\nPaste the <b>contract address</b> of the token you want to buy.\n"
        "You'll see its price, liquidity and holders first, then choose how much to buy."
    ), InlineKeyboardMarkup(inline_keyboard=[
        [_b("🔍 Explore coins", "exp:refresh")],
        [_b("❌ Cancel", "menu:home")],
    ]))


@router.message(DashFlow.buy_ca)
async def buy_receive_ca(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not addr_utils.is_valid(text):
        await message.answer("❌ That doesn't look like a valid TON contract address. Paste it again, or /cancel.")
        return
    await state.clear()
    loading = await message.answer("🔎 Looking up token…")
    await trade_handlers.show_token_card(message, text, loading=loading)


# ---- 🔴 Sell: show holdings, pick one ---------------------------------------------------

@router.callback_query(F.data == "dash:sell")
async def sell_pick(callback: CallbackQuery, state: FSMContext):
    wallet = await require_wallet(callback)
    if not wallet:
        return
    await callback.answer()
    await state.clear()
    try:
        holds = await ton_client.get_jetton_holdings(wallet.address)
        prices = await ton_client.get_usd_prices([h["contract"] for h in holds])
    except Exception:
        await edit(callback.message, "⚠️ Couldn't load your holdings. Please try again in a few seconds.",
                   InlineKeyboardMarkup(inline_keyboard=[[_b("🔄 Try again", "dash:sell"), _b("🏠 Home", "menu:home")]]))
        return
    if not holds:
        await edit(callback.message, "🔴 <b>Sell</b>\n\nYou don't hold any tokens yet. Buy one first, or deposit some.",
                   InlineKeyboardMarkup(inline_keyboard=[[_b("🟢 Buy", "dash:buy"), _b("📥 Deposit", "dep:open")],
                                                         [_b("🏠 Home", "menu:home")]]))
        return

    rows = []
    for h in holds:
        p = prices.get(h["raw"])
        rows.append((h, h["balance"] * p if p else None))
    rows.sort(key=lambda r: r[1] or 0, reverse=True)
    rows = rows[:20]

    lines = ["🔴 <b>Sell</b>\n\nYour holdings — tap a token to sell it:\n"]
    for h, usd in rows:
        lines.append(f"• <b>{esc(h['symbol'])}</b> — {fmt_amount(h['balance'])}" + (f" (≈ {fmt_usd(usd)})" if usd else " (no price)"))
    buttons = [_b(f"🔴 {h['symbol'][:12]}", f"hs:{h['contract']}") for h, _ in rows]
    kb = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    kb.append([_b("🔄 Refresh", "dash:sell"), _b("🏠 Home", "menu:home")])
    await edit(callback.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb))
