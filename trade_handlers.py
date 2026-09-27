from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from db import async_session, User, Wallet, Trade
import ton_client
import dex
import fees
import encryption
from balance_handlers import _get_primary_wallet
from start_handlers import main_menu_kb

router = Router()


class TradeFlow(StatesGroup):
    waiting_for_ca = State()
    waiting_for_amount = State()
    waiting_for_large_trade_confirm = State()


# ---- Entry points ----

@router.callback_query(F.data.in_({"menu:buy", "menu:sell", "menu:swap"}))
async def trade_start(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]  # buy | sell | swap
    await state.update_data(action=action)

    if action == "sell" or action == "swap":
        # offer holdings to pick from, or CA entry
        wallet = await _get_primary_wallet(callback.from_user.id)
        holdings = await ton_client.get_jetton_holdings(wallet.address) if wallet else []
        rows = [
            [InlineKeyboardButton(text=f"{h['symbol']}", callback_data=f"trade:pick:{h['contract']}")]
            for h in holdings[:10]
        ]
        rows.append([InlineKeyboardButton(text="✏️ Enter contract address", callback_data="trade:enter_ca")])
        rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="menu:home")])
        label = "sell" if action == "sell" else "swap"
        await callback.message.edit_text(
            f"Select a token to {label}, or enter a contract address (CA):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    else:
        # buy: only CA entry makes sense (or from holdings shortcut, handled in balance.py)
        await callback.message.edit_text(
            "🟢 <b>Buy</b>\n\nSend the token's contract address (CA) you want to buy.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back", callback_data="menu:home")]
            ]),
        )
        await state.set_state(TradeFlow.waiting_for_ca)

    await callback.answer()


@router.callback_query(F.data == "trade:enter_ca")
async def trade_enter_ca(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TradeFlow.waiting_for_ca)
    await callback.message.edit_text("Send the token's contract address (CA):")
    await callback.answer()


@router.callback_query(F.data.startswith("trade:pick:"))
async def trade_pick_holding(callback: CallbackQuery, state: FSMContext):
    contract = callback.data.split(":", 2)[2]
    await state.update_data(contract=contract)
    market = await ton_client.get_token_market_data(contract)
    if market is None:
        await callback.answer("Couldn't load token data.", show_alert=True)
        return
    await _show_token_details(callback, state, contract, market)


@router.callback_query(F.data.startswith("buy:holding:"))
async def buy_from_holding(callback: CallbackQuery, state: FSMContext):
    contract = callback.data.split(":", 2)[2]
    await state.update_data(action="buy", contract=contract)
    market = await ton_client.get_token_market_data(contract)
    if market is None:
        await callback.answer("Couldn't load token data.", show_alert=True)
        return
    await _show_token_details(callback, state, contract, market)


@router.callback_query(F.data.startswith("sell:holding:"))
async def sell_from_holding(callback: CallbackQuery, state: FSMContext):
    contract = callback.data.split(":", 2)[2]
    await state.update_data(action="sell", contract=contract)
    market = await ton_client.get_token_market_data(contract)
    if market is None:
        await callback.answer("Couldn't load token data.", show_alert=True)
        return
    await _show_token_details(callback, state, contract, market)


@router.message(TradeFlow.waiting_for_ca)
async def trade_receive_ca(message: Message, state: FSMContext):
    contract = message.text.strip()
    market = await ton_client.get_token_market_data(contract)
    if market is None:
        await message.answer("❌ Couldn't find a token at that address. Double-check and try again, or /cancel.")
        return
    await state.update_data(contract=contract)
    await _show_token_details(message, state, contract, market)


async def _show_token_details(event, state: FSMContext, contract: str, market: dict):
    data = await state.get_data()
    action = data.get("action", "buy")

    def fmt_usd(v):
        if v is None:
            return "—"
        v = float(v)
        if v >= 1_000_000:
            return f"${v/1_000_000:.2f}M"
        if v >= 1_000:
            return f"${v/1_000:.1f}K"
        return f"${v:.4f}" if v < 1 else f"${v:,.2f}"

    def fmt_price(v):
        if v is None:
            return "—"
        v = float(v)
        return f"${v:.8f}" if v < 0.01 else f"${v:,.4f}"

    change = market.get("price_change_24h_pct")
    change_str = f"{change:+.2f}%" if change is not None else "—"
    verified_badge = "✅" if market.get("verified") else "⚠️ Unverified"

    text = (
        f"<b>{market['name']} (${market['symbol']})</b> {verified_badge}\n"
        f"<code>{contract}</code>\n\n"
        f"💵 Price: {fmt_price(market['price_usd'])}\n"
        f"📊 Market Cap: {fmt_usd(market['market_cap_usd'])}\n"
        f"💧 Liquidity: {fmt_usd(market['liquidity_usd'])}\n"
        f"📈 24h Volume: {fmt_usd(market['volume_24h_usd'])}\n"
        f"📉 24h Change: {change_str}\n"
        f"👥 Holders: {market.get('holders_count', '—')}\n"
        f"🏦 DEX: {market.get('dex') or '—'}\n"
    )

    if market["liquidity_usd"] is None:
        text += "\n⚠️ <i>No liquidity pool found for this token. It may be too new, illiquid, or the address may be wrong — trading may fail or slip heavily.</i>\n"

    text += "\nProceed?"

    kb_rows = [[InlineKeyboardButton(text=f"➡️ Continue to {action.capitalize()}", callback_data="trade:proceed")]]
    if market.get("pair_url"):
        kb_rows.append([InlineKeyboardButton(text="🔍 View chart", url=market["pair_url"])])
    kb_rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="menu:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(F.data == "trade:proceed")
async def trade_proceed_to_amount(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _ask_amount(callback, state, data["contract"])


async def _ask_amount(event, state: FSMContext, contract: str, meta: dict | None = None):
    data = await state.get_data()
    action = data.get("action", "buy")
    meta = meta or await ton_client.get_token_metadata(contract)
    symbol = meta["symbol"] if meta else "token"

    await state.update_data(contract=contract)
    await state.set_state(TradeFlow.waiting_for_amount)

    unit = "TON" if action == "buy" else symbol
    text = (
        f"How much {unit} do you want to {action}?\n\n"
        f"Reply with an amount (e.g. <code>2.5</code>), or <code>max</code> for your full balance.\n"
        f"A 5% fee applies to every trade."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:home")]
    ])

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.message(TradeFlow.waiting_for_amount)
async def trade_receive_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    action = data["action"]
    contract = data["contract"]

    wallet = await _get_primary_wallet(message.from_user.id)
    if not wallet:
        await message.answer("No wallet found.")
        await state.clear()
        return

    # resolve amount
    raw = message.text.strip().lower()
    if raw == "max":
        if action == "buy":
            amount = await ton_client.get_ton_balance(wallet.address)
        else:
            holdings = await ton_client.get_jetton_holdings(wallet.address)
            match = next((h for h in holdings if h["contract"] == contract), None)
            amount = match["balance"] if match else 0
    else:
        try:
            amount = float(raw)
        except ValueError:
            await message.answer("Please send a number, or 'max'.")
            return

    if amount <= 0:
        await message.answer("Amount must be greater than 0.")
        return

    # For buy: amount is in TON. For sell/swap: amount is in the token being sold.
    # Fee is calculated on the TON-denominated leg — for buy that's straightforward;
    # for sell, fee should be taken from the TON received after the swap executes.
    amount_ton_equivalent = amount  # TODO: for sell, convert token amount -> TON via quote first

    allowed, reason = await fees.check_trade_allowed(amount_ton_equivalent, user_daily_volume_ton=0)
    if not allowed:
        await message.answer(f"❌ {reason}")
        await state.clear()
        return

    needs_confirm = await fees.needs_large_trade_confirmation(amount_ton_equivalent)
    await state.update_data(amount=amount)

    if needs_confirm:
        await state.set_state(TradeFlow.waiting_for_large_trade_confirm)
        await message.answer(
            f"⚠️ This is a large trade (~{amount_ton_equivalent:.2f} TON). "
            f"Reply <b>CONFIRM</b> to proceed, or /cancel to abort."
        )
        return

    await _execute_trade(message, state)


@router.message(TradeFlow.waiting_for_large_trade_confirm)
async def trade_large_confirm(message: Message, state: FSMContext):
    if message.text.strip().upper() != "CONFIRM":
        await message.answer("Not confirmed. Send CONFIRM to proceed, or /cancel.")
        return
    await _execute_trade(message, state)


async def _execute_trade(message: Message, state: FSMContext):
    data = await state.get_data()
    action, contract, amount = data["action"], data["contract"], data["amount"]

    fee_config = await fees.get_fee_config()
    fee_amount, amount_after_fee = fees.calculate_fee(amount, fee_config.fee_bps)

    await message.answer(
        f"⏳ Processing {action}…\n"
        f"Amount: {amount}\n"
        f"Fee ({fee_config.fee_bps / 100:.1f}%): {fee_amount:.6f}\n"
        f"Net: {amount_after_fee:.6f}"
    )

    # ---- This is where dex.get_quote / dex.execute_swap plug in ----
    # try:
    #     wallet = await _get_primary_wallet(message.from_user.id)
    #     async with async_session() as session:
    #         w = await session.get(Wallet, wallet.id)
    #         mnemonic = encryption.decrypt_mnemonic(w.encrypted_mnemonic, w.wrapped_data_key)
    #     quote = await dex.get_quote(token_in=..., token_out=..., amount_in=amount_after_fee, slippage_bps=...)
    #     tx_hash = await dex.execute_swap(mnemonic, quote)
    #     fee_tx_hash = await send_fee_to_dev_wallet(mnemonic, fee_amount, fee_config.dev_wallet)
    #     record Trade row with status=success
    # except Exception as e:
    #     record Trade row with status=failed, notify user
    # -----------------------------------------------------------------

    await message.answer(
        "🚧 DEX execution is not wired up in this scaffold yet (see IMPORTANT.md, "
        "section 6). Once services/dex.py is connected to STON.fi/DeDust, this "
        "message is replaced by a real success/failure result with tx hash.",
        reply_markup=main_menu_kb(has_wallet=True),
    )
    await state.clear()
