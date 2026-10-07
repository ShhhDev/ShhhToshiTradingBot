"""
Automated trading screens: 🗒 Limit Orders, 📸 Copy Trade, 🎯 Snipes.
Everything here only CREATES/CANCELS orders. workers.py watches the market and fires them
through executor.run_swap(), so the admin-set fee applies to every fill.
"""

import time

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, update

import addr_utils
import dex
import fees
import ton_client
from db import async_session, LimitOrder, CopyTrade, Snipe
from helpers import (
    edit, esc, fmt_amount, fmt_pct_bps, fmt_price, get_or_create_user, reply_or_edit, require_wallet, short_addr,
)

router = Router()
TON = "TON"
DEXES = {"stonfi": "STON.fi", "dedust": "DeDust"}


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def _cancel():
    return [_btn("❌ Cancel", "menu:home")]


def _not_live_note() -> str:
    return "" if dex.LIVE else "\n\n🚧 <i>The trading engine isn't switched on yet — orders are saved but won't fire until it is.</i>"


async def _fee_line() -> str:
    fc = await fees.get_fee_config()
    return f"\nFee on each fill: {fmt_pct_bps(fc.fee_bps)}"


async def _user_id(tg_user) -> int:
    return (await get_or_create_user(tg_user)).id


def _num(text: str) -> float | None:
    try:
        v = float((text or "").strip().replace(",", "."))
    except ValueError:
        return None
    return v if v == v and v > 0 and v != float("inf") else None


# =========================================================================================
# 🗒 LIMIT ORDERS
# =========================================================================================

class LimitFlow(StatesGroup):
    token = State()
    price = State()
    amount = State()


async def send_limit_orders(event):
    if isinstance(event, CallbackQuery):
        await event.answer()
    uid = await _user_id(event.from_user)
    async with async_session() as s:
        orders = (await s.execute(select(LimitOrder).where(
            LimitOrder.user_id == uid, LimitOrder.status.in_(("open", "filling"))).order_by(LimitOrder.id))).scalars().all()
    lines = ["🗒 <b>Limit Orders</b>\n"]
    for o in orders:
        cond = "≤" if o.side == "buy" else "≥"
        amt = f"{fmt_amount(o.amount)} TON" if o.side == "buy" else f"{fmt_amount(o.amount)} {esc(o.symbol)}"
        lines.append(f"#{o.id} {o.side.upper()} {esc(o.symbol)} when price {cond} {fmt_price(float(o.trigger_price_usd))} · {amt}")
    if not orders:
        lines.append("No open orders.")
    kb = [[_btn("➕ New Order", "lo:new")]]
    kb += [[_btn(f"🗑 Cancel #{o.id}", f"lo:x:{o.id}")] for o in orders[:10]]
    kb.append([_btn("⬅️ Back", "menu:home"), _btn("🛑 Stop All", "lo:stopall")])
    await reply_or_edit(event, "\n".join(lines) + await _fee_line() + _not_live_note(), InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data == "lo:new")
async def lo_new(callback: CallbackQuery, state: FSMContext):
    if not await require_wallet(callback):
        return
    await callback.answer()
    await state.clear()
    await state.set_state(LimitFlow.token)
    await edit(callback.message, "🗒 Enter the <b>token contract address</b> for the limit order:",
               InlineKeyboardMarkup(inline_keyboard=[[_btn("🔍 Explore coins", "exp:refresh")], _cancel()]))


@router.message(LimitFlow.token)
async def lo_token(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not addr_utils.is_valid(text):
        await message.answer("❌ That isn't a valid contract address. Paste it again, or /cancel.")
        return
    meta = await ton_client.get_token_metadata(text)
    if meta is None:
        await message.answer("❌ Couldn't find a token at that address.")
        return
    await state.update_data(token=meta["contract"], symbol=meta["symbol"][:30])
    await message.answer(f"<b>{esc(meta['symbol'])}</b> — buy or sell?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🟢 Buy (price drops to…)", "lo:side:buy"), _btn("🔴 Sell (price rises to…)", "lo:side:sell")], _cancel()]))


@router.callback_query(F.data.startswith("lo:side:"), LimitFlow.token)
async def lo_side(callback: CallbackQuery, state: FSMContext):
    side = callback.data.split(":")[2]
    if side not in ("buy", "sell"):
        await callback.answer()
        return
    await callback.answer()
    d = await state.get_data()
    prices = await ton_client.get_usd_prices([d["token"]])
    now = prices.get(addr_utils.to_raw(d["token"]))
    await state.update_data(side=side)
    await state.set_state(LimitFlow.price)
    await edit(callback.message,
               f"Current price: {fmt_price(now)}\n\nSend the <b>trigger price in USD</b> "
               f"(the order {'buys' if side == 'buy' else 'sells'} when the price is "
               f"{'at or below' if side == 'buy' else 'at or above'} it):",
               InlineKeyboardMarkup(inline_keyboard=[_cancel()]))


@router.message(LimitFlow.price)
async def lo_price(message: Message, state: FSMContext):
    price = _num(message.text)
    if price is None:
        await message.answer("Send a positive number like <code>0.0025</code>.")
        return
    d = await state.get_data()
    await state.update_data(price=price)
    await state.set_state(LimitFlow.amount)
    ask = "How many <b>TON</b> to spend?" if d["side"] == "buy" else \
        "How much to sell? Send an amount, or a percentage of your holdings like <code>50%</code>."
    await message.answer(ask, reply_markup=InlineKeyboardMarkup(inline_keyboard=[_cancel()]))


@router.message(LimitFlow.amount)
async def lo_amount(message: Message, state: FSMContext):
    d = await state.get_data()
    wallet = await require_wallet(message)
    if not wallet:
        return
    raw = (message.text or "").strip()
    if d["side"] == "sell" and raw.endswith("%"):
        pct = _num(raw[:-1])
        if pct is None or pct > 100:
            await message.answer("Percentage must be between 0 and 100.")
            return
        held = [h for h in await ton_client.get_jetton_holdings(wallet.address) if addr_utils.same(h["contract"], d["token"])]
        if not held:
            await message.answer(f"❌ You don't hold any {esc(d['symbol'])} right now. Send a fixed amount instead.")
            return
        amount = held[0]["balance"] * pct / 100
    else:
        amount = _num(raw)
    if not amount:
        await message.answer("Send a positive number.")
        return
    uid = await _user_id(message.from_user)
    async with async_session() as s:
        o = LimitOrder(user_id=uid, wallet_id=wallet.id, token=d["token"], symbol=d["symbol"], side=d["side"],
                       trigger_price_usd=d["price"], amount=amount)
        s.add(o)
        await s.commit()
        oid = o.id
    await state.clear()
    await message.answer(f"✅ Limit order #{oid} saved.{_not_live_note()}")
    await send_limit_orders(message)


@router.callback_query(F.data.startswith("lo:x:"))
async def lo_cancel(callback: CallbackQuery):
    uid = await _user_id(callback.from_user)
    async with async_session() as s:
        await s.execute(update(LimitOrder).where(
            LimitOrder.id == int(callback.data[5:]), LimitOrder.user_id == uid, LimitOrder.status == "open"
        ).values(status="cancelled"))
        await s.commit()
    await send_limit_orders(callback)


@router.callback_query(F.data == "lo:stopall")
async def lo_stop_all(callback: CallbackQuery):
    uid = await _user_id(callback.from_user)
    async with async_session() as s:
        await s.execute(update(LimitOrder).where(LimitOrder.user_id == uid, LimitOrder.status == "open").values(status="cancelled"))
        await s.commit()
    await send_limit_orders(callback)


# =========================================================================================
# 📸 COPY TRADE
# =========================================================================================

class CopyFlow(StatesGroup):
    trader = State()
    amount = State()


async def send_copy_trades(event):
    if isinstance(event, CallbackQuery):
        await event.answer()
    uid = await _user_id(event.from_user)
    async with async_session() as s:
        rows = (await s.execute(select(CopyTrade).where(CopyTrade.user_id == uid, CopyTrade.active.is_(True)).order_by(CopyTrade.id))).scalars().all()
    lines = ["📸 <b>Copy Trade Orders</b>\n"]
    for c in rows:
        lines.append(f"#{c.id} {short_addr(c.trader_address)} · buys {fmt_amount(c.buy_amount_ton)} TON each")
    if not rows:
        lines.append("No active copy orders.")
    lines.append("\nWhen the trader buys a token you buy the fixed TON amount; when they sell, you sell your whole position in it. "
                 "Only trades made after you add the order are copied.")
    kb = [[_btn("➕ New Order", "cp:new")]]
    kb += [[_btn(f"🗑 Remove #{c.id}", f"cp:x:{c.id}")] for c in rows[:10]]
    kb.append([_btn("⬅️ Back", "menu:home"), _btn("🛑 Stop All", "cp:stopall")])
    await reply_or_edit(event, "\n".join(lines) + await _fee_line() + _not_live_note(), InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data == "cp:new")
async def cp_new(callback: CallbackQuery, state: FSMContext):
    if not await require_wallet(callback):
        return
    await callback.answer()
    await state.clear()
    await state.set_state(CopyFlow.trader)
    await edit(callback.message, "📸 Enter the <b>trader address</b> you want to copy:", InlineKeyboardMarkup(inline_keyboard=[_cancel()]))


@router.message(CopyFlow.trader)
async def cp_trader(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    wallet = await require_wallet(message)
    if not wallet:
        return
    if not addr_utils.is_valid(text):
        await message.answer("❌ That isn't a valid address. Paste it again, or /cancel.")
        return
    if addr_utils.same(text, wallet.address):
        await message.answer("❌ You can't copy your own wallet.")
        return
    await state.update_data(trader=addr_utils.canonical(text))
    await state.set_state(CopyFlow.amount)
    await message.answer("How many <b>TON</b> should each copied buy spend?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[_cancel()]))


@router.message(CopyFlow.amount)
async def cp_amount(message: Message, state: FSMContext):
    amount = _num(message.text)
    wallet = await require_wallet(message)
    if not wallet:
        return
    if amount is None:
        await message.answer("Send a positive number like <code>1</code>.")
        return
    d = await state.get_data()
    uid = await _user_id(message.from_user)
    async with async_session() as s:
        s.add(CopyTrade(user_id=uid, wallet_id=wallet.id, trader_address=d["trader"], buy_amount_ton=amount, last_ts=int(time.time())))
        await s.commit()
    await state.clear()
    await message.answer("✅ Copy order created.")
    await send_copy_trades(message)


@router.callback_query(F.data.startswith("cp:x:"))
async def cp_remove(callback: CallbackQuery):
    uid = await _user_id(callback.from_user)
    async with async_session() as s:
        await s.execute(update(CopyTrade).where(CopyTrade.id == int(callback.data[5:]), CopyTrade.user_id == uid).values(active=False))
        await s.commit()
    await send_copy_trades(callback)


@router.callback_query(F.data == "cp:stopall")
async def cp_stop_all(callback: CallbackQuery):
    uid = await _user_id(callback.from_user)
    async with async_session() as s:
        await s.execute(update(CopyTrade).where(CopyTrade.user_id == uid).values(active=False))
        await s.commit()
    await send_copy_trades(callback)


# =========================================================================================
# 🎯 SNIPES
# =========================================================================================

class SnipeFlow(StatesGroup):
    target = State()
    value = State()


async def send_snipes(event):
    if isinstance(event, CallbackQuery):
        await event.answer()
    uid = await _user_id(event.from_user)
    async with async_session() as s:
        rows = (await s.execute(select(Snipe).where(Snipe.user_id == uid, Snipe.active.is_(True)).order_by(Snipe.id))).scalars().all()
    lines = ["🎯 <b>Snipes management</b>\n"]
    for sn in rows:
        lines.append(f"#{sn.id} {'Deployer' if sn.kind == 'deployer' else 'Jetton'} {short_addr(sn.target)} · {fmt_amount(sn.amount_ton)} TON")
    if not rows:
        lines.append("No active snipes.")
    kb = [[_btn("Snipe Deployer 🕵️", "sn:new:deployer"), _btn("Snipe Jetton 🪙", "sn:new:jetton")]]
    kb += [[_btn(f"🗑 Cancel #{sn.id}", f"sn:x:{sn.id}")] for sn in rows[:10]]
    kb.append([_btn("⬅️ Back", "menu:home"), _btn("🛑 Stop All", "sn:stopall")])
    await reply_or_edit(event, "\n".join(lines) + await _fee_line() + _not_live_note(), InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("sn:new:"))
async def sn_new(callback: CallbackQuery, state: FSMContext):
    kind = callback.data[7:]
    if kind not in ("jetton", "deployer") or not await require_wallet(callback):
        return
    await callback.answer()
    await state.clear()
    await state.set_state(SnipeFlow.target)
    await state.update_data(kind=kind, amount=0.0, slippage_bps=2500, dexes=[])
    what = "Jetton master address you want to snipe" if kind == "jetton" else "Deployer wallet address to watch for new jettons"
    await edit(callback.message, f"🎯 Enter {what}:", InlineKeyboardMarkup(inline_keyboard=[_cancel()]))


@router.message(SnipeFlow.target)
async def sn_target(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not addr_utils.is_valid(text):
        await message.answer("❌ That isn't a valid address. Paste it again, or /cancel.")
        return
    target = addr_utils.canonical(text)
    d = await state.get_data()
    label = target
    if d["kind"] == "jetton":
        meta = await ton_client.get_token_metadata(target)
        if meta is None:
            await message.answer("❌ That isn't a jetton master address.")
            return
        label = f"${esc(meta['symbol'])} · {esc(meta['name'])}"
    await state.update_data(target=target, label=label)
    await state.set_state(None)
    await _snipe_config(message, state)


async def _snipe_config(event, state: FSMContext):
    d = await state.get_data()
    chosen = set(d.get("dexes", []))
    text = (f"🎯 <b>Snipe {'Jetton' if d['kind'] == 'jetton' else 'Deployer'}</b>\n\n{d.get('label', '')}\n<code>{d['target']}</code>\n\n"
            f"Amount: <b>{fmt_amount(d['amount'])} TON</b>\nSlippage: <b>{d['slippage_bps'] / 100:g}%</b>\n"
            f"DEXes: {', '.join(DEXES[k] for k in chosen) if chosen else 'any'}" + await _fee_line() + _not_live_note())
    kb = [[_btn(f"{name} {'✅' if k in chosen else ''}".strip(), f"sn:dex:{k}") for k, name in DEXES.items()],
          [_btn(f"Slippage {d['slippage_bps'] / 100:g}% ✏️", "sn:set:slip"), _btn(f"Amount {fmt_amount(d['amount'])} ✏️", "sn:set:amt")],
          [_btn("✅ Start snipe", "sn:start"), _btn("🚫 Cancel", "menu:home")]]
    await reply_or_edit(event, text, InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("sn:dex:"))
async def sn_dex(callback: CallbackQuery, state: FSMContext):
    key = callback.data[7:]
    d = await state.get_data()
    if key not in DEXES or "target" not in d:
        await callback.answer("This screen expired.", show_alert=True)
        return
    chosen = set(d.get("dexes", []))
    chosen ^= {key}
    await state.update_data(dexes=sorted(chosen))
    await callback.answer()
    await _snipe_config(callback, state)


@router.callback_query(F.data.startswith("sn:set:"))
async def sn_set(callback: CallbackQuery, state: FSMContext):
    field = callback.data[7:]
    if field not in ("slip", "amt") or "target" not in await state.get_data():
        await callback.answer("This screen expired.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(SnipeFlow.value)
    await state.update_data(field=field)
    await edit(callback.message, "Send the slippage in % (e.g. <code>25</code>):" if field == "slip" else "Send the amount in TON:",
               InlineKeyboardMarkup(inline_keyboard=[_cancel()]))


@router.message(SnipeFlow.value)
async def sn_value(message: Message, state: FSMContext):
    v = _num(message.text)
    d = await state.get_data()
    if v is None or (d["field"] == "slip" and v > 100):
        await message.answer("Send a valid positive number.")
        return
    await state.update_data(**({"slippage_bps": int(v * 100)} if d["field"] == "slip" else {"amount": v}))
    await state.set_state(None)
    await _snipe_config(message, state)


@router.callback_query(F.data == "sn:start")
async def sn_start(callback: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    wallet = await require_wallet(callback)
    if not wallet:
        return
    if "target" not in d:
        await callback.answer("This screen expired.", show_alert=True)
        return
    if d["amount"] <= 0:
        await callback.answer("Set an amount first.", show_alert=True)
        return
    await callback.answer()
    uid = await _user_id(callback.from_user)
    async with async_session() as s:
        s.add(Snipe(user_id=uid, wallet_id=wallet.id, kind=d["kind"], target=d["target"], amount_ton=d["amount"],
                    slippage_bps=d["slippage_bps"], dexes=",".join(d.get("dexes", [])), last_ts=int(time.time())))
        await s.commit()
    await state.clear()
    await send_snipes(callback)


@router.callback_query(F.data.startswith("sn:x:"))
async def sn_cancel(callback: CallbackQuery):
    uid = await _user_id(callback.from_user)
    async with async_session() as s:
        await s.execute(update(Snipe).where(Snipe.id == int(callback.data[5:]), Snipe.user_id == uid).values(active=False, status="cancelled"))
        await s.commit()
    await send_snipes(callback)


@router.callback_query(F.data == "sn:stopall")
async def sn_stop_all(callback: CallbackQuery):
    uid = await _user_id(callback.from_user)
    async with async_session() as s:
        await s.execute(update(Snipe).where(Snipe.user_id == uid, Snipe.active.is_(True)).values(active=False, status="cancelled"))
        await s.commit()
    await send_snipes(callback)
