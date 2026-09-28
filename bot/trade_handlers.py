"""
Swap / buy / sell flows.

  💎 TON  <->  any token    (buy / sell)
  token   <->  token        (swap)

All of them share one pipeline:  pick tokens -> amount -> review -> confirm.
Tokens are picked from your holdings or by pasting a contract address (CA).
Pasting a CA at any time (outside a flow) opens a token card with Buy / Sell.

Quotes and on-chain swaps run through STON.fi (dex.get_quote / execute_swap).
Confirm signs with the custodial wallet key, broadcasts the swap, collects
the bot fee when input is TON, and records a Trade row.
"""

import logging
import math

from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import addr_utils
import dex
import encryption
import fees
import ton_client
from db import async_session, Trade
from config import config
from helpers import (
    edit, esc, fmt_amount, fmt_big_usd, fmt_pct_bps, fmt_price, get_active_wallet,
    get_or_create_user, is_admin, reply_or_edit, require_wallet,
)

logger = logging.getLogger(__name__)
router = Router()

TON = "TON"
EPS = 1e-9
PRESETS = (25, 50, 75, 100)


class TradeFlow(StatesGroup):
    waiting_for_ca = State()
    waiting_for_amount = State()
    confirming = State()


# ---- small helpers ----------------------------------------------------------------

def _valid_token(token: str) -> bool:
    return token == TON or addr_utils.is_valid(token)


async def _token_info(token: str) -> dict | None:
    if token == TON:
        return {"symbol": "TON", "name": "Toncoin", "decimals": 9, "verification": "whitelist", "contract": TON}
    return await ton_client.get_token_metadata(token)


async def _token_balance(wallet_address: str, token: str) -> float:
    for h in await ton_client.get_jetton_holdings(wallet_address):
        if addr_utils.same(h["contract"], token):
            return h["balance"]
    return 0.0


async def _ton_value(token: str, amount: float) -> float | None:
    """Best-effort TON-equivalent of an amount (None if the token can't be priced)."""
    if token == TON:
        return amount
    prices = await ton_client.get_usd_prices([token])
    token_usd, ton_usd = prices.get(addr_utils.to_raw(token)), prices.get("TON")
    if token_usd and ton_usd:
        return amount * token_usd / ton_usd
    return None


def _floor(x: float, decimals: int) -> float:
    scale = 10 ** decimals
    return math.floor(x * scale) / scale


async def _fail(event, error: Exception):
    logger.error("trade flow: data provider error", exc_info=error)
    text = (
        "⚠️ <b>Couldn't load that right now.</b>\n\n"
        "The blockchain data provider didn't respond. Please try again in a few seconds."
    )
    if is_admin(event.from_user.id):
        text += f"\n\n<i>Admin detail:</i> <code>{esc(str(error) or type(error).__name__)[:300]}</code>"
    await reply_or_edit(event, text, None)


def _cancel_row() -> list:
    return [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:home")]


def _grid(buttons: list, per_row: int = 2) -> list:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


# ---- 1. token card (paste a CA, or tap a token in Balance) --------------------------

def _card_text(m: dict, held: float) -> str:
    verification = m.get("verification", "none")
    badge = {"whitelist": "✅", "blacklist": "🚫 SCAM"}.get(verification, "⚠️ Unverified")
    change = m.get("price_change_24h_pct")
    holders = m.get("holders_count")
    text = (
        f"🪙 <b>{esc(m['name'])} (${esc(m['symbol'])})</b> {badge}\n"
        f"<code>{m['contract']}</code>\n\n"
        f"💵 Price: {fmt_price(m['price_usd'])}\n"
        f"📊 Market Cap: {fmt_big_usd(m['market_cap_usd'])}\n"
        f"💧 Liquidity: {fmt_big_usd(m['liquidity_usd'])}\n"
        f"📈 24h Volume: {fmt_big_usd(m['volume_24h_usd'])}\n"
        f"📉 24h Change: {f'{float(change):+.2f}%' if change is not None else '—'}\n"
        f"👥 Holders: {f'{int(holders):,}' if holders is not None else '—'}\n"
        f"🏦 DEX: {esc(m.get('dex') or '—')}\n"
    )
    if held > 0:
        text += f"\n👛 You hold: <b>{fmt_amount(held)} {esc(m['symbol'])}</b>\n"
    if verification == "blacklist":
        text += "\n🚫 <b>Flagged as a scam token — buying it is blocked.</b>\n"
    elif m["liquidity_usd"] is None:
        text += (
            "\n⚠️ <i>No liquidity pool found for this token. It may be brand new, illiquid, "
            "or the address may be wrong — trades may fail or slip heavily.</i>\n"
        )
    return text


async def show_token_card(event, token: str, loading: Message | None = None):
    token = addr_utils.canonical(token)
    target = loading if loading else (event.message if isinstance(event, CallbackQuery) else None)

    async def show(text, kb=None):
        if target:
            await edit(target, text, kb)
        else:
            await event.answer(text, reply_markup=kb)

    wallet = await get_active_wallet(event.from_user.id)
    try:
        market = await ton_client.get_token_market_data(token)
        held = await _token_balance(wallet.address, token) if wallet else 0.0
    except Exception as e:
        logger.error("token card failed", exc_info=e)
        await show("⚠️ Couldn't load that token right now. Please try again in a few seconds.")
        return

    if market is None:
        await show("❌ Couldn't find a token at that address. Double-check it and try again.")
        return

    trade_row = []
    if market.get("verification") != "blacklist":
        trade_row.append(InlineKeyboardButton(text="🟢 Buy", callback_data=f"hb:{token}"))
    if held > 0:
        trade_row.append(InlineKeyboardButton(text="🔴 Sell", callback_data=f"hs:{token}"))
    rows = [trade_row] if trade_row else []
    if held > 0:
        rows.append([InlineKeyboardButton(text="🔁 Swap this token", callback_data=f"sf:{token}")])
    if market.get("pair_url"):
        rows.append([InlineKeyboardButton(text="🔍 View chart", url=market["pair_url"])])
    if isinstance(event, CallbackQuery):
        rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="bal:refresh")])
    else:
        rows.append([InlineKeyboardButton(text="✖️ Close", callback_data="msg:close")])
    await show(_card_text(market, held), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("tk:"))
async def open_token_from_button(callback: CallbackQuery, state: FSMContext):
    token = callback.data[3:]
    if not addr_utils.is_valid(token):
        await callback.answer()
        return
    await state.clear()
    await callback.answer()
    await show_token_card(callback, token)


def _is_address_message(message: Message) -> bool:
    return bool(message.text) and addr_utils.is_valid(message.text.strip())


@router.message(StateFilter(None), _is_address_message)
async def pasted_address(message: Message):
    """A contract address pasted outside any flow opens that token's card."""
    wallet = await require_wallet(message)
    if not wallet:
        return
    if addr_utils.same(message.text.strip(), wallet.address):
        await message.answer("That's your own wallet address. Tap 📥 Deposit to fund it.")
        return
    loading = await message.answer("🔎 Looking up token…")
    await show_token_card(message, message.text.strip(), loading=loading)


@router.callback_query(F.data == "msg:close")
async def close_message(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


# ---- 2. swap: pick FROM, pick TO ------------------------------------------------------


async def start_buy(event, state: FSMContext):
    """🟢 Buy — spend TON for a token. User pastes a CA or picks from holdings later."""
    wallet = await require_wallet(event)
    if not wallet:
        return
    await state.clear()
    await state.set_state(TradeFlow.waiting_for_ca)
    await state.update_data(buy_mode=True, from_token=TON)
    kb = InlineKeyboardMarkup(inline_keyboard=[_cancel_row()])
    await reply_or_edit(
        event,
        "🟢 <b>Buy</b>\n\n"
        "Paste the <b>contract address (CA)</b> of the token you want to buy with TON.\n\n"
        "Or open 💰 Balance / paste a CA anytime for the token card with Buy.",
        kb,
    )


async def start_sell(event, state: FSMContext):
    """🔴 Sell — pick a held token to sell for TON."""
    wallet = await require_wallet(event)
    if not wallet:
        return
    await state.clear()
    if isinstance(event, CallbackQuery):
        await event.answer()

    try:
        holdings = await ton_client.get_jetton_holdings(wallet.address)
    except Exception as e:
        await _fail(event, e)
        return

    if not holdings:
        await reply_or_edit(
            event,
            "🔴 <b>Sell</b>\n\nYou don't hold any jettons yet.\n"
            "Deposit tokens or buy some first, then tap 🔴 Sell again.",
            InlineKeyboardMarkup(inline_keyboard=[_cancel_row()]),
        )
        return

    buttons = [
        InlineKeyboardButton(text=f"🪙 {h['symbol'][:14]}", callback_data=f"hs:{h['contract']}")
        for h in holdings[:20]
    ]
    rows = _grid(buttons) + [_cancel_row()]
    await reply_or_edit(
        event,
        "🔴 <b>Sell</b>\n\nChoose the token you want to sell for TON:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def start_swap(event, state: FSMContext):
    """🔁 Swap menu button / deposit-notification button."""
    wallet = await require_wallet(event)
    if not wallet:
        return
    await state.clear()
    if isinstance(event, CallbackQuery):
        await event.answer()

    try:
        holdings = await ton_client.get_jetton_holdings(wallet.address)
    except Exception as e:
        await _fail(event, e)
        return

    buttons = [InlineKeyboardButton(text="💎 TON", callback_data=f"sf:{TON}")]
    buttons += [
        InlineKeyboardButton(text=f"🪙 {h['symbol'][:14]}", callback_data=f"sf:{h['contract']}")
        for h in holdings[:20]
    ]
    rows = _grid(buttons) + [_cancel_row()]
    await reply_or_edit(
        event,
        "🔁 <b>Swap</b>\n\nStep 1 of 2 — choose the token you want to <b>swap from</b>:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "swap:start")
async def swap_start_callback(callback: CallbackQuery, state: FSMContext):
    await start_swap(callback, state)


@router.callback_query(F.data.startswith("sf:"))
async def swap_pick_from(callback: CallbackQuery, state: FSMContext):
    token = callback.data[3:]
    if not _valid_token(token):
        await callback.answer()
        return
    wallet = await require_wallet(callback)
    if not wallet:
        return
    await callback.answer()

    try:
        info = await _token_info(token)
        holdings = await ton_client.get_jetton_holdings(wallet.address)
    except Exception as e:
        await _fail(callback, e)
        return
    if info is None:
        await edit(callback.message, "❌ Couldn't load that token. Tap 🔁 Swap to try again.")
        return

    # from now on a pasted contract address is taken as the token to receive
    await state.set_state(TradeFlow.waiting_for_ca)
    await state.update_data(from_token=token)

    buttons = []
    if token != TON:
        buttons.append(InlineKeyboardButton(text="💎 TON", callback_data=f"st:{TON}"))
    buttons += [
        InlineKeyboardButton(text=f"🪙 {h['symbol'][:14]}", callback_data=f"st:{h['contract']}")
        for h in holdings[:20] if not addr_utils.same(h["contract"], token)
    ]
    rows = _grid(buttons) + [
        [InlineKeyboardButton(text="✏️ Enter contract address", callback_data="sca")],
        _cancel_row(),
    ]
    await edit(
        callback.message,
        f"🔁 <b>Swap from {esc(info['symbol'])}</b>\n\n"
        "Step 2 of 2 — choose what you want to <b>receive</b>, or just paste a token "
        "contract address (CA) in the chat:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "sca")
async def swap_enter_ca(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("from_token"):
        await callback.answer("This swap expired — tap 🔁 Swap to start again.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(TradeFlow.waiting_for_ca)
    await edit(
        callback.message,
        "✏️ Paste the contract address (CA) of the token you want to receive:",
        InlineKeyboardMarkup(inline_keyboard=[_cancel_row()]),
    )


@router.callback_query(F.data.startswith("st:"))
async def swap_pick_to(callback: CallbackQuery, state: FSMContext):
    to_token = callback.data[3:]
    from_token = (await state.get_data()).get("from_token")
    if not from_token or not _valid_token(to_token):
        await callback.answer("This swap expired — tap 🔁 Swap to start again.", show_alert=True)
        return
    if to_token == from_token or addr_utils.same(to_token, from_token):
        await callback.answer("Pick a different token to receive.", show_alert=True)
        return
    await callback.answer()
    await _go_amount(callback, state, from_token, to_token)


@router.message(TradeFlow.waiting_for_ca)
async def swap_receive_ca(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not addr_utils.is_valid(text):
        await message.answer("❌ That doesn't look like a valid TON contract address. Paste it again, or /cancel.")
        return
    from_token = (await state.get_data()).get("from_token")
    if not from_token:
        await state.clear()
        await message.answer("That swap expired — tap 🔁 Swap to start again.")
        return
    to_token = addr_utils.canonical(text)
    if addr_utils.same(to_token, from_token):
        await message.answer("That's the token you're swapping from. Paste a different one, or /cancel.")
        return
    await _go_amount(message, state, from_token, to_token)


@router.callback_query(F.data.startswith("hb:"))
async def buy_token(callback: CallbackQuery, state: FSMContext):
    token = callback.data[3:]
    if not addr_utils.is_valid(token):
        await callback.answer()
        return
    await callback.answer()
    await state.clear()
    await _go_amount(callback, state, TON, addr_utils.canonical(token))


@router.callback_query(F.data.startswith("hs:"))
async def sell_token(callback: CallbackQuery, state: FSMContext):
    token = callback.data[3:]
    if not addr_utils.is_valid(token):
        await callback.answer()
        return
    await callback.answer()
    await state.clear()
    await _go_amount(callback, state, addr_utils.canonical(token), TON)


# ---- 3. amount ----------------------------------------------------------------------

async def _go_amount(event, state: FSMContext, from_token: str, to_token: str):
    """Loads balances and shows the amount screen. Never answers the callback (callers do)."""
    wallet = await require_wallet(event)
    if not wallet:
        return

    try:
        from_info = await _token_info(from_token)
        to_info = await _token_info(to_token)
        ton_bal = await ton_client.get_ton_balance(wallet.address)
        bal = ton_bal if from_token == TON else await _token_balance(wallet.address, from_token)
        fc = await fees.get_fee_config()
    except Exception as e:
        await _fail(event, e)
        return

    if from_info is None or to_info is None:
        await reply_or_edit(event, "❌ Couldn't find that token. Check the address and try again.")
        return
    if to_info.get("verification") == "blacklist":
        await reply_or_edit(event, "🚫 That token is flagged as a scam, so buying it is blocked.")
        return

    from_sym, to_sym = esc(from_info["symbol"]), esc(to_info["symbol"])
    reserve = config.GAS_RESERVE_TON

    if from_token == TON:
        usable = max(bal - reserve, 0.0)
        if usable <= 0:
            await reply_or_edit(
                event,
                f"❌ <b>Not enough TON.</b>\n\nYou have {fmt_amount(bal)} TON, and ~{reserve:g} TON is "
                "always kept for network fees. Tap 📥 Deposit to add more.",
            )
            return
    else:
        usable = bal
        if bal <= 0:
            await reply_or_edit(event, f"❌ You don't hold any {from_sym}.")
            return
        if ton_bal < reserve:
            await reply_or_edit(
                event,
                f"❌ <b>Not enough TON for network fees.</b>\n\nYou need at least ~{reserve:g} TON in the "
                f"wallet to trade {from_sym} (you have {fmt_amount(ton_bal)} TON). Tap 📥 Deposit to add some.",
            )
            return

    await state.set_state(TradeFlow.waiting_for_amount)
    await state.update_data(
        from_token=from_token, to_token=to_token, bal=bal, usable=usable,
        from_dec=int(from_info.get("decimals", 9)),
    )

    text = f"🔁 <b>{from_sym} → {to_sym}</b>\n\nBalance: <b>{fmt_amount(bal)} {from_sym}</b>\n"
    if from_token == TON:
        text += f"Available to trade: {fmt_amount(usable)} TON <i>(~{reserve:g} TON is kept for network fees)</i>\n"
    text += f"Fee: {fmt_pct_bps(fc.fee_bps)} of the amount\n\nType an amount, or tap a preset:"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("Max" if p == 100 else f"{p}%"), callback_data=f"amt:{p}") for p in PRESETS],
        _cancel_row(),
    ])
    await reply_or_edit(event, text, kb)


@router.message(TradeFlow.waiting_for_amount)
async def amount_typed(message: Message, state: FSMContext):
    raw = (message.text or "").strip().lower().replace(",", ".")
    data = await state.get_data()
    if raw in ("max", "all"):
        amount = float(data.get("usable", 0))
    else:
        try:
            amount = float(raw)
        except ValueError:
            await message.answer("Please send a number like <code>2.5</code>, or tap a preset button.")
            return
    await _validate_and_review(message, state, amount, is_preset=raw in ("max", "all"))


@router.callback_query(F.data.startswith("amt:"), TradeFlow.waiting_for_amount)
async def amount_preset(callback: CallbackQuery, state: FSMContext):
    try:
        pct = int(callback.data[4:])
    except ValueError:
        await callback.answer()
        return
    if pct not in PRESETS:
        await callback.answer()
        return
    await callback.answer()
    usable = float((await state.get_data()).get("usable", 0))
    await _validate_and_review(callback, state, usable * pct / 100, is_preset=True)


@router.callback_query(F.data.startswith("amt:"))
async def amount_preset_stale(callback: CallbackQuery):
    await callback.answer("This screen expired — tap 🔁 Swap to start again.", show_alert=True)


# ---- 4. review + confirm ------------------------------------------------------------

async def _validate_and_review(event, state: FSMContext, amount: float, is_preset: bool):
    data = await state.get_data()
    from_token, to_token = data.get("from_token"), data.get("to_token")
    if not from_token or not to_token:
        await state.clear()
        await reply_or_edit(event, "That swap expired — tap 🔁 Swap to start again.")
        return

    usable = float(data.get("usable", 0))
    decimals = min(int(data.get("from_dec", 9)), 9)
    from_info, to_info = await _token_info(from_token), await _token_info(to_token)
    from_sym = esc(from_info["symbol"]) if from_info else "token"
    to_sym = esc(to_info["symbol"]) if to_info else "token"

    if not math.isfinite(amount) or amount <= 0:
        await reply_or_edit(event, "Amount must be greater than 0. Type a number, or tap a preset.")
        return
    if is_preset:
        amount = min(_floor(amount, decimals), usable)
        if amount <= 0:
            await reply_or_edit(event, "That amount is too small. Try a larger one.")
            return
    if amount > usable + EPS:
        if from_token == TON:
            msg = (
                f"❌ <b>Not enough TON.</b> You can trade up to {fmt_amount(usable)} TON "
                f"(balance {fmt_amount(data.get('bal', 0))} TON, with ~{config.GAS_RESERVE_TON:g} TON kept for network fees)."
            )
        else:
            msg = f"❌ <b>Not enough {from_sym}.</b> You have {fmt_amount(usable)} {from_sym}."
        await reply_or_edit(event, msg + "\n\nType a smaller amount, or tap a preset.")
        return

    ton_value = await _ton_value(from_token, amount)
    if ton_value is not None:
        allowed, reason = await fees.check_trade_allowed(ton_value, user_daily_volume_ton=0)
    else:
        allowed, reason = await fees.check_trade_allowed(0, 0)  # trade caps need a price; kill switch still applies
    if not allowed:
        await state.clear()
        await reply_or_edit(event, f"❌ {esc(reason)}")
        return
    large = ton_value is not None and await fees.needs_large_trade_confirmation(ton_value)

    fc = await fees.get_fee_config()
    fee_amount, net = fees.calculate_fee(amount, fc.fee_bps)
    user = await get_or_create_user(event.from_user)

    from_dec = int((from_info or {}).get("decimals", 9) if from_info else data.get("from_dec", 9))
    to_dec = int((to_info or {}).get("decimals", 9))

    # Live STON.fi quote for the amount that will actually be swapped (after fee)
    quote_line = ""
    quote_error = None
    try:
        quote = await dex.get_quote(
            token_in=from_token,
            token_out=to_token,
            amount_in=net,
            slippage_bps=int(user.slippage_bps),
            decimals_in=from_dec,
            decimals_out=to_dec,
        )
        quote_line = (
            f"You receive (est.): <b>{fmt_amount(quote.amount_out_estimated, 6)} {to_sym}</b>\n"
            f"Min received: {fmt_amount(quote.min_amount_out, 6)} {to_sym}\n"
            f"Price impact: {quote.price_impact_pct:.3f}%\n"
            f"Route: {esc(quote.route)}\n"
        )
    except Exception as e:
        logger.warning("quote failed during review: %s", e)
        quote_error = str(e) or type(e).__name__
        quote_line = (
            f"You receive (est.): <i>quote unavailable</i>\n"
            f"<i>{esc(quote_error)[:120]}</i>\n"
        )

    text = (
        "🧾 <b>Review swap</b>\n\n"
        f"You send: <b>{fmt_amount(amount, 6)} {from_sym}</b>\n"
        f"Fee ({fmt_pct_bps(fc.fee_bps)}): {fmt_amount(fee_amount, 6)} {from_sym}\n"
        f"Swapped: {fmt_amount(net, 6)} {from_sym} → <b>{to_sym}</b>\n"
        f"{quote_line}"
        f"Slippage: {user.slippage_bps / 100:g}%\n"
        "Network fees are paid in TON from your wallet."
    )
    if large:
        text += "\n\n⚠️ <b>Large trade</b> — please double-check the amount before confirming."
    if quote_error:
        text += "\n\n⚠️ Could not get a live quote — trade may still be blocked at confirm."

    await state.set_state(TradeFlow.confirming)
    await state.update_data(amount=amount, large=large, from_dec=from_dec, to_dec=to_dec)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Yes, confirm large trade" if large else "✅ Confirm",
            callback_data="trade:confirm",
        )],
        [InlineKeyboardButton(text="✏️ Change amount", callback_data="trade:edit")],
        _cancel_row(),
    ])
    await reply_or_edit(event, text, kb)


@router.callback_query(F.data == "trade:edit", TradeFlow.confirming)
async def edit_amount(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.answer()
    await _go_amount(callback, state, data.get("from_token", ""), data.get("to_token", ""))


@router.callback_query(F.data == "trade:confirm", TradeFlow.confirming)
async def confirm_trade(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()  # a second tap on the same button can no longer trigger a second trade
    await callback.answer()
    await _execute_trade(callback, data)


@router.callback_query(F.data.in_({"trade:confirm", "trade:edit"}))
async def confirm_stale(callback: CallbackQuery):
    await callback.answer("This confirmation expired — tap 🔁 Swap to start again.", show_alert=True)


async def _execute_trade(callback: CallbackQuery, data: dict):
    from_token, to_token, amount = data.get("from_token"), data.get("to_token"), data.get("amount")
    wallet = await get_active_wallet(callback.from_user.id)
    if not (wallet and from_token and to_token and amount):
        await edit(callback.message, "That swap expired — tap 🔁 Swap to start again.")
        return

    fc = await fees.get_fee_config()
    if not fc.trading_enabled:
        await edit(callback.message, "⏸ Trading is paused right now. Please try again later.")
        return

    # fresh balance check right before executing (balances may have moved since the review)
    try:
        ton_bal = await ton_client.get_ton_balance(wallet.address)
        bal = ton_bal if from_token == TON else await _token_balance(wallet.address, from_token)
    except Exception as e:
        await _fail(callback, e)
        return
    reserve = config.GAS_RESERVE_TON
    enough = (amount + reserve <= ton_bal + EPS) if from_token == TON else (amount <= bal + EPS and ton_bal >= reserve)
    if not enough:
        await edit(callback.message, "❌ Your balance changed and it no longer covers this trade. Nothing was sent.")
        return

    fee_amount, net = fees.calculate_fee(amount, fc.fee_bps)
    user = await get_or_create_user(callback.from_user)
    from_dec = int(data.get("from_dec", 9))
    to_dec = int(data.get("to_dec", 9))
    await edit(callback.message, "⏳ Processing your swap…")

    trade_type = "buy" if from_token == TON else ("sell" if to_token == TON else "swap")
    explorer = (
        "https://tonviewer.com" if config.TON_NETWORK == "mainnet" else "https://testnet.tonviewer.com"
    )

    try:
        quote = await dex.get_quote(
            token_in=from_token,
            token_out=to_token,
            amount_in=net,
            slippage_bps=int(user.slippage_bps),
            decimals_in=from_dec,
            decimals_out=to_dec,
        )

        mnemonic = encryption.decrypt_mnemonic(wallet.encrypted_mnemonic, wallet.wrapped_data_key)
        tx_hash = await dex.execute_swap(
            wallet_mnemonic=mnemonic,
            quote=quote,
            user_wallet_address=wallet.address,
            decimals_in=from_dec,
        )

        # Collect fee in TON when the input is TON (most common buy path).
        # For jetton sells the fee is already taken as a reduced swap amount;
        # a separate jetton fee transfer is intentionally skipped for simplicity.
        fee_tx = None
        if from_token == TON and fee_amount > 0 and fc.dev_wallet:
            try:
                fee_tx = await dex.send_ton(
                    wallet_mnemonic=mnemonic,
                    to_address=fc.dev_wallet,
                    amount_ton=fee_amount,
                    from_address=wallet.address,
                    comment="ShhhToshi fee",
                )
            except Exception as fee_err:
                logger.warning("fee transfer failed (swap already sent): %s", fee_err)

        # Record the trade
        async with async_session() as session:
            session.add(Trade(
                user_id=user.id,
                wallet_address=wallet.address,
                trade_type=trade_type,
                token_in=from_token,
                token_out=to_token,
                amount_in=float(amount),
                amount_out=float(quote.amount_out_estimated),
                fee_bps_applied=int(fc.fee_bps),
                fee_amount_ton=float(fee_amount if from_token == TON else 0),
                fee_tx_hash=fee_tx,
                tx_hash=tx_hash,
                status="success",
            ))
            await session.commit()

        from_info = await _token_info(from_token)
        to_info = await _token_info(to_token)
        from_sym = esc((from_info or {}).get("symbol") or from_token[:8])
        to_sym = esc((to_info or {}).get("symbol") or to_token[:8])

        link = f"{explorer}/transaction/{tx_hash}" if tx_hash and tx_hash != "submitted" else explorer
        text = (
            f"✅ <b>Swap submitted</b>\n\n"
            f"Sent: <b>{fmt_amount(amount, 6)} {from_sym}</b>\n"
            f"Fee: {fmt_amount(fee_amount, 6)} {from_sym}\n"
            f"Est. receive: <b>{fmt_amount(quote.amount_out_estimated, 6)} {to_sym}</b>\n"
            f"Route: {esc(quote.route)}\n"
            f"Tx: <code>{esc(str(tx_hash)[:64])}</code>\n"
            f'<a href="{link}">View on explorer</a>\n\n'
            "Balances update after the network confirms the transaction."
        )
        await edit(callback.message, text)

    except Exception as e:
        logger.error("swap failed", exc_info=e)
        # best-effort failed trade row
        try:
            async with async_session() as session:
                session.add(Trade(
                    user_id=user.id,
                    wallet_address=wallet.address,
                    trade_type=trade_type,
                    token_in=from_token,
                    token_out=to_token,
                    amount_in=float(amount),
                    amount_out=0,
                    fee_bps_applied=int(fc.fee_bps),
                    fee_amount_ton=0,
                    status="failed",
                ))
                await session.commit()
        except Exception:
            pass
        msg = (
            "❌ The swap couldn't be completed.\n\n"
            f"<i>{esc(str(e) or type(e).__name__)[:240]}</i>\n\n"
            "Please check your balance and try again."
        )
        if is_admin(callback.from_user.id):
            msg += f"\n\n<code>{esc(repr(e))[:300]}</code>"
        await edit(callback.message, msg)
