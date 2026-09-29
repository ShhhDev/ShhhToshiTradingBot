"""
Referral program.

  - Every user gets a permanent referral code + a t.me deep link on first /start.
  - A new user who opens the bot via someone's link (/start CODE) is bound to
    that referrer forever (first link wins; cannot be changed later).
  - On every completed trade, a cut of the platform fee is credited to the
    trader's referrer (see award_referral_credit(), called from trade_handlers).
    The share is admin-configurable (FeeConfig.referral_share_bps); the example
    in the spec is 10% of the fee, e.g. a 10 TON fee credits 1 TON to the referrer.
  - Referrers request a payout with a Claim button once their balance clears
    the admin-configurable minimum; an admin approves or declines it by hand
    (see admin_handlers.py) — nothing is paid out automatically.
"""

import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func

import fees as fee_service
from config import config
from db import async_session, User, ClaimRequest
from helpers import edit, esc, fmt_amount, get_active_wallet, get_or_create_user, is_admin, reply_or_edit

logger = logging.getLogger(__name__)
router = Router()

_bot_username_cache: dict = {"value": None}


class ClaimFlow(StatesGroup):
    confirming = State()


async def _bot_username(bot) -> str:
    if _bot_username_cache["value"] is None:
        me = await bot.get_me()
        _bot_username_cache["value"] = me.username
    return _bot_username_cache["value"]


async def referral_link(bot, code: str) -> str:
    username = await _bot_username(bot)
    return f"https://t.me/{username}?start={code}"


async def award_referral_credit(fee_amount_ton: float, trader_telegram_id: int):
    """
    Credits the trader's referrer with a share of a just-charged trade fee.
    Called by trade_handlers right after a trade's fee is actually collected.
    Safe to call even if the trader has no referrer (no-op) or is self-referred
    (blocked at bind time, but double-checked here too).
    """
    if fee_amount_ton <= 0:
        return
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == trader_telegram_id))
        trader = result.scalar_one_or_none()
        if trader is None or trader.referred_by_id is None or trader.referred_by_id == trader.id:
            return

        referrer = await session.get(User, trader.referred_by_id)
        if referrer is None or referrer.is_banned:
            return

        fc = await fee_service.get_fee_config()
        credit = fee_amount_ton * (fc.referral_share_bps / 10_000)
        if credit <= 0:
            return

        referrer.referral_balance_ton = float(referrer.referral_balance_ton) + credit
        referrer.referral_earned_total_ton = float(referrer.referral_earned_total_ton) + credit
        await session.commit()
        logger.info(f"referral credit: +{credit:.9f} TON to user {referrer.telegram_id} "
                    f"(from trader {trader_telegram_id}'s fee of {fee_amount_ton})")


# ---- screen -------------------------------------------------------------------

async def build_referral_screen(bot, tg_user) -> tuple[str, InlineKeyboardMarkup]:
    user = await get_or_create_user(tg_user)
    async with async_session() as session:
        referred_count = (await session.execute(
            select(func.count(User.id)).where(User.referred_by_id == user.id)
        )).scalar_one()
        pending = (await session.execute(
            select(ClaimRequest).where(ClaimRequest.user_id == user.id, ClaimRequest.status == "pending")
        )).scalars().first()

    fc = await fee_service.get_fee_config()
    link = await referral_link(bot, user.referral_code)
    share_pct = fc.referral_share_bps / 100
    min_claim = float(fc.referral_min_claim_ton)
    balance = float(user.referral_balance_ton)

    text = (
        "🎁 <b>Referral Program</b>\n\n"
        f"Earn <b>{share_pct:g}%</b> of the trading fee every time someone you invite trades.\n\n"
        f"🔗 Your link:\n<code>{esc(link)}</code>\n\n"
        f"👥 Referred: <b>{referred_count}</b>\n"
        f"💰 Available to claim: <b>{fmt_amount(balance, 4)} TON</b>\n"
        f"📈 Lifetime earned: {fmt_amount(float(user.referral_earned_total_ton), 4)} TON\n"
        f"✅ Lifetime claimed: {fmt_amount(float(user.referral_claimed_total_ton), 4)} TON\n"
    )

    rows = [[InlineKeyboardButton(text="🔄 Refresh", callback_data="ref:refresh")]]
    if pending:
        text += (
            f"\n⏳ <i>You have a pending claim for {fmt_amount(float(pending.amount_ton), 4)} TON "
            "awaiting admin review.</i>"
        )
    elif balance + 1e-9 >= min_claim > 0:
        rows.insert(0, [InlineKeyboardButton(text=f"💸 Claim {fmt_amount(balance, 4)} TON", callback_data="ref:claim")])
    else:
        text += f"\n<i>Minimum claim is {fmt_amount(min_claim, 4)} TON.</i>"

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def send_referral(event):
    if isinstance(event, CallbackQuery):
        await event.answer()
    text, kb = await build_referral_screen(event.bot, event.from_user)
    await reply_or_edit(event, text, kb)


@router.callback_query(F.data == "ref:refresh")
async def refresh_referral(callback: CallbackQuery):
    await send_referral(callback)


# ---- claim ----------------------------------------------------------------------

@router.callback_query(F.data == "ref:claim")
async def claim_start(callback: CallbackQuery, state: FSMContext):
    user = await get_or_create_user(callback.from_user)
    fc = await fee_service.get_fee_config()
    balance = float(user.referral_balance_ton)
    min_claim = float(fc.referral_min_claim_ton)

    if balance + 1e-9 < min_claim:
        await callback.answer(f"Minimum claim is {min_claim:g} TON.", show_alert=True)
        return

    async with async_session() as session:
        existing = (await session.execute(
            select(ClaimRequest).where(ClaimRequest.user_id == user.id, ClaimRequest.status == "pending")
        )).scalars().first()
    if existing:
        await callback.answer("You already have a pending claim.", show_alert=True)
        return

    wallet = await get_active_wallet(callback.from_user.id)
    if not wallet:
        await callback.answer("Set up a wallet first so we know where to send your claim.", show_alert=True)
        return

    await callback.answer()
    await state.set_state(ClaimFlow.confirming)
    await state.update_data(amount=balance, address=wallet.address)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Submit claim request", callback_data="ref:claim_confirm")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="ref:refresh")],
    ])
    await edit(
        callback.message,
        f"💸 <b>Claim {fmt_amount(balance, 4)} TON</b>\n\n"
        f"Payout wallet:\n<code>{wallet.address}</code>\n\n"
        "This sends a request to the admin for review. Your balance will show as pending "
        "until it's approved or declined.",
        kb,
    )


@router.callback_query(F.data == "ref:claim_confirm", ClaimFlow.confirming)
async def claim_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    amount, address = data.get("amount"), data.get("address")
    await state.clear()
    await callback.answer()

    if not amount or not address:
        await edit(callback.message, "That claim request expired. Tap 🎁 Referral to try again.")
        return

    user = await get_or_create_user(callback.from_user)
    async with async_session() as session:
        # re-check under a fresh read: balance may have moved, and this blocks a double-submit
        fresh = await session.get(User, user.id)
        if float(fresh.referral_balance_ton) + 1e-9 < float(amount):
            await edit(callback.message, "Your balance changed — tap 🎁 Referral to see the current amount.")
            return
        existing = (await session.execute(
            select(ClaimRequest).where(ClaimRequest.user_id == fresh.id, ClaimRequest.status == "pending")
        )).scalars().first()
        if existing:
            await edit(callback.message, "You already have a pending claim.")
            return

        claim = ClaimRequest(user_id=fresh.id, amount_ton=amount, payout_address=address, status="pending")
        session.add(claim)
        await session.commit()
        await session.refresh(claim)

    await edit(
        callback.message,
        f"✅ <b>Claim submitted.</b>\n\n{fmt_amount(amount, 4)} TON is pending admin review. "
        "You'll be notified here once it's approved or declined.",
    )

    for admin_id in config.ADMIN_TELEGRAM_IDS:
        try:
            await callback.bot.send_message(
                admin_id,
                f"🔔 <b>New referral claim</b>\n\n"
                f"User: <code>{callback.from_user.id}</code>"
                + (f" (@{esc(callback.from_user.username)})" if callback.from_user.username else "") + "\n"
                f"Amount: {fmt_amount(amount, 4)} TON\n"
                f"Payout address:\n<code>{address}</code>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Approve", callback_data=f"adm:claim:approve:{claim.id}"),
                    InlineKeyboardButton(text="❌ Decline", callback_data=f"adm:claim:decline:{claim.id}"),
                ]]),
            )
        except Exception as e:
            logger.warning(f"failed to notify admin {admin_id} of claim {claim.id}: {e}")


@router.callback_query(F.data == "ref:claim_confirm")
async def claim_confirm_stale(callback: CallbackQuery):
    await callback.answer("This claim request expired — tap 🎁 Referral to try again.", show_alert=True)
