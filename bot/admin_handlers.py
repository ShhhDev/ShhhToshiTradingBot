"""
Admin controls, live inside the bot — no separate website, no second
Railway service. Only Telegram user IDs listed in ADMIN_TELEGRAM_IDS
(.env) can open this menu; everyone else gets silently ignored so the
bot doesn't even reveal that /admin exists.
"""

import logging
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, func

from config import config
from db import async_session, FeeConfig, User, Wallet, Trade, AdminAuditLog, ClaimRequest
from helpers import esc

logger = logging.getLogger(__name__)
router = Router()


def is_admin(telegram_id: int) -> bool:
    return telegram_id in config.ADMIN_TELEGRAM_IDS


class AdminEdit(StatesGroup):
    waiting_for_fee_pct = State()
    waiting_for_dev_wallet = State()
    waiting_for_max_trade = State()
    waiting_for_max_daily = State()
    waiting_for_confirm_threshold = State()
    waiting_for_referral_share = State()
    waiting_for_referral_min_claim = State()
    waiting_for_decline_reason = State()


async def _log_action(admin_id: int, action: str, detail: str):
    async with async_session() as session:
        session.add(AdminAuditLog(admin_telegram_id=admin_id, action=action, detail=detail))
        await session.commit()


def admin_menu_kb(fc: FeeConfig) -> InlineKeyboardMarkup:
    status = "🟢 LIVE" if fc.trading_enabled else "🔴 PAUSED"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Fee: {fc.fee_bps/100:.1f}% (tap to change)", callback_data="admin:edit_fee")],
        [InlineKeyboardButton(text="Dev wallet (tap to change)", callback_data="admin:edit_wallet")],
        [InlineKeyboardButton(text=f"Max trade: {float(fc.max_trade_ton):g} TON", callback_data="admin:edit_max_trade")],
        [InlineKeyboardButton(text=f"Max daily/user: {float(fc.max_daily_volume_ton):g} TON", callback_data="admin:edit_max_daily")],
        [InlineKeyboardButton(text=f"Confirm threshold: {float(fc.large_trade_confirm_threshold_ton):g} TON", callback_data="admin:edit_threshold")],
        [InlineKeyboardButton(text=f"Referral share: {fc.referral_share_bps/100:g}% of fee", callback_data="admin:edit_ref_share")],
        [InlineKeyboardButton(text=f"Referral min claim: {float(fc.referral_min_claim_ton):g} TON", callback_data="admin:edit_ref_min")],
        [InlineKeyboardButton(text=f"Trading: {status} (tap to toggle)", callback_data="admin:toggle_trading")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="admin:stats")],
        [InlineKeyboardButton(text="💸 Pending claims", callback_data="admin:claims")],
        [InlineKeyboardButton(text="📜 Audit log", callback_data="admin:audit")],
    ])


async def _get_fc() -> FeeConfig:
    async with async_session() as session:
        result = await session.execute(select(FeeConfig).where(FeeConfig.id == 1))
        return result.scalar_one()


@router.message(F.text == "/admin")
async def admin_home(message: Message):
    if not is_admin(message.from_user.id):
        return  # silently ignore — don't reveal this command exists to non-admins
    fc = await _get_fc()
    await message.answer(
        "🔐 <b>Admin Panel</b>\n\nTap any setting to change it.",
        reply_markup=admin_menu_kb(fc),
    )


@router.callback_query(F.data == "admin:home")
async def admin_home_cb(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    fc = await _get_fc()
    await callback.message.edit_text(
        "🔐 <b>Admin Panel</b>\n\nTap any setting to change it.",
        reply_markup=admin_menu_kb(fc),
    )
    await callback.answer()


# ---- Toggle trading (no confirm text needed, single tap + re-render) ----

@router.callback_query(F.data == "admin:toggle_trading")
async def admin_toggle_trading(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()

    async with async_session() as session:
        result = await session.execute(select(FeeConfig).where(FeeConfig.id == 1))
        fc = result.scalar_one()
        fc.trading_enabled = not fc.trading_enabled
        fc.updated_at = datetime.utcnow()
        fc.updated_by_admin_id = callback.from_user.id
        await session.commit()
        new_state = fc.trading_enabled

    await _log_action(callback.from_user.id, "toggle_trading", f"trading_enabled -> {new_state}")
    await callback.answer(f"Trading {'resumed' if new_state else 'paused'}.")
    fc = await _get_fc()
    await callback.message.edit_text("🔐 <b>Admin Panel</b>\n\nTap any setting to change it.", reply_markup=admin_menu_kb(fc))


# ---- Fee % ----

@router.callback_query(F.data == "admin:edit_fee")
async def admin_edit_fee(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await state.set_state(AdminEdit.waiting_for_fee_pct)
    await callback.message.edit_text(
        "Send the new fee percentage (e.g. <code>5</code> for 5%, <code>2.5</code> for 2.5%).\n"
        "Applies to every buy, sell, and swap. Send /cancel to abort.",
    )
    await callback.answer()


@router.message(AdminEdit.waiting_for_fee_pct)
async def admin_receive_fee(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        pct = float(message.text.strip())
        if not (0 <= pct <= 100):
            raise ValueError
    except ValueError:
        await message.answer("Send a number between 0 and 100 (e.g. 5 or 2.5), or /cancel.")
        return

    new_bps = int(round(pct * 100))
    async with async_session() as session:
        result = await session.execute(select(FeeConfig).where(FeeConfig.id == 1))
        fc = result.scalar_one()
        old_bps = fc.fee_bps
        fc.fee_bps = new_bps
        fc.updated_at = datetime.utcnow()
        fc.updated_by_admin_id = message.from_user.id
        await session.commit()

    await _log_action(message.from_user.id, "update_fee", f"{old_bps} -> {new_bps} bps")
    await state.clear()
    fc = await _get_fc()
    await message.answer(f"✅ Fee updated to {pct}%.", reply_markup=admin_menu_kb(fc))


# ---- Dev wallet ----

@router.callback_query(F.data == "admin:edit_wallet")
async def admin_edit_wallet(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await state.set_state(AdminEdit.waiting_for_dev_wallet)
    await callback.message.edit_text(
        "Send the new dev fee wallet address.\n\n"
        "⚠️ Triple-check this. TON transactions are irreversible — a typo "
        "here sends every future fee to whatever address you paste, "
        "permanently. Send /cancel to abort.",
    )
    await callback.answer()


@router.message(AdminEdit.waiting_for_dev_wallet)
async def admin_receive_wallet(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    new_wallet = message.text.strip()
    if len(new_wallet) < 20:  # crude sanity check, not real address validation
        await message.answer("That doesn't look like a valid TON address. Try again, or /cancel.")
        return

    async with async_session() as session:
        result = await session.execute(select(FeeConfig).where(FeeConfig.id == 1))
        fc = result.scalar_one()
        old_wallet = fc.dev_wallet
        fc.dev_wallet = new_wallet
        fc.updated_at = datetime.utcnow()
        fc.updated_by_admin_id = message.from_user.id
        await session.commit()

    await _log_action(message.from_user.id, "update_dev_wallet", f"{old_wallet} -> {new_wallet}")
    await state.clear()
    fc = await _get_fc()
    await message.answer(
        f"✅ Dev wallet updated.\n<code>{new_wallet}</code>\n\nDouble-check it's correct.",
        reply_markup=admin_menu_kb(fc),
    )


# ---- Max trade / max daily / confirm threshold — same pattern ----

async def _generic_numeric_setting(message: Message, state: FSMContext, field: str, label: str, admin_menu_kb_, unit: str = "TON"):
    if not is_admin(message.from_user.id):
        return
    try:
        value = float(message.text.strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer(f"Send a number ≥ 0 (0 = no limit), or /cancel.")
        return

    async with async_session() as session:
        result = await session.execute(select(FeeConfig).where(FeeConfig.id == 1))
        fc = result.scalar_one()
        old_value = getattr(fc, field)
        setattr(fc, field, value)
        fc.updated_at = datetime.utcnow()
        fc.updated_by_admin_id = message.from_user.id
        await session.commit()

    await _log_action(message.from_user.id, f"update_{field}", f"{old_value} -> {value}")
    await state.clear()
    fc = await _get_fc()
    await message.answer(f"✅ {label} updated to {value:g} {unit}.", reply_markup=admin_menu_kb_(fc))


@router.callback_query(F.data == "admin:edit_max_trade")
async def admin_edit_max_trade(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await state.set_state(AdminEdit.waiting_for_max_trade)
    await callback.message.edit_text("Send max trade size in TON (0 = no limit). /cancel to abort.")
    await callback.answer()


@router.message(AdminEdit.waiting_for_max_trade)
async def admin_receive_max_trade(message: Message, state: FSMContext):
    await _generic_numeric_setting(message, state, "max_trade_ton", "Max trade size", admin_menu_kb)


@router.callback_query(F.data == "admin:edit_max_daily")
async def admin_edit_max_daily(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await state.set_state(AdminEdit.waiting_for_max_daily)
    await callback.message.edit_text("Send max daily volume per user in TON (0 = no limit). /cancel to abort.")
    await callback.answer()


@router.message(AdminEdit.waiting_for_max_daily)
async def admin_receive_max_daily(message: Message, state: FSMContext):
    await _generic_numeric_setting(message, state, "max_daily_volume_ton", "Max daily volume", admin_menu_kb)


@router.callback_query(F.data == "admin:edit_threshold")
async def admin_edit_threshold(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await state.set_state(AdminEdit.waiting_for_confirm_threshold)
    await callback.message.edit_text(
        "Send the large-trade confirmation threshold in TON (0 = never ask for manual CONFIRM). /cancel to abort."
    )
    await callback.answer()


@router.message(AdminEdit.waiting_for_confirm_threshold)
async def admin_receive_threshold(message: Message, state: FSMContext):
    await _generic_numeric_setting(
        message, state, "large_trade_confirm_threshold_ton", "Confirmation threshold", admin_menu_kb
    )


# ---- Referral share / minimum claim ----

@router.callback_query(F.data == "admin:edit_ref_share")
async def admin_edit_ref_share(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await state.set_state(AdminEdit.waiting_for_referral_share)
    await callback.message.edit_text(
        "Send the referral share as a percentage of the trade fee "
        "(e.g. <code>10</code> for 10% — a 10 TON fee then credits 1 TON to the referrer). /cancel to abort."
    )
    await callback.answer()


@router.message(AdminEdit.waiting_for_referral_share)
async def admin_receive_ref_share(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        pct = float(message.text.strip())
        if not (0 <= pct <= 100):
            raise ValueError
    except ValueError:
        await message.answer("Send a number between 0 and 100, or /cancel.")
        return

    new_bps = int(round(pct * 100))
    async with async_session() as session:
        result = await session.execute(select(FeeConfig).where(FeeConfig.id == 1))
        fc = result.scalar_one()
        old_bps = fc.referral_share_bps
        fc.referral_share_bps = new_bps
        fc.updated_at = datetime.utcnow()
        fc.updated_by_admin_id = message.from_user.id
        await session.commit()

    await _log_action(message.from_user.id, "update_referral_share", f"{old_bps} -> {new_bps} bps")
    await state.clear()
    fc = await _get_fc()
    await message.answer(f"✅ Referral share updated to {pct:g}% of the fee.", reply_markup=admin_menu_kb(fc))


@router.callback_query(F.data == "admin:edit_ref_min")
async def admin_edit_ref_min(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await state.set_state(AdminEdit.waiting_for_referral_min_claim)
    await callback.message.edit_text("Send the minimum referral balance (in TON) needed to submit a claim. /cancel to abort.")
    await callback.answer()


@router.message(AdminEdit.waiting_for_referral_min_claim)
async def admin_receive_ref_min(message: Message, state: FSMContext):
    await _generic_numeric_setting(message, state, "referral_min_claim_ton", "Referral minimum claim", admin_menu_kb)


# ---- Stats ----

@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()

    async with async_session() as session:
        user_count = (await session.execute(select(func.count(User.id)))).scalar_one()
        wallet_count = (await session.execute(select(func.count(Wallet.id)))).scalar_one()
        trade_count = (await session.execute(select(func.count(Trade.id)))).scalar_one()

        since_24h = datetime.utcnow() - timedelta(hours=24)
        trades_24h = (await session.execute(
            select(func.count(Trade.id)).where(Trade.created_at >= since_24h)
        )).scalar_one()
        fees_24h = (await session.execute(
            select(func.coalesce(func.sum(Trade.fee_amount_ton), 0)).where(Trade.created_at >= since_24h)
        )).scalar_one()
        fees_total = (await session.execute(
            select(func.coalesce(func.sum(Trade.fee_amount_ton), 0))
        )).scalar_one()

    text = (
        "📊 <b>Stats</b>\n\n"
        f"Users: {user_count}\n"
        f"Wallets: {wallet_count}\n"
        f"Total trades: {trade_count}\n"
        f"Trades (24h): {trades_24h}\n"
        f"Fees collected (24h): {fees_24h:.4f} TON\n"
        f"Fees collected (all-time): {fees_total:.4f} TON\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="admin:home")]])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# ---- Audit log ----

@router.callback_query(F.data == "admin:audit")
async def admin_audit(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()

    async with async_session() as session:
        result = await session.execute(
            select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(15)
        )
        logs = result.scalars().all()

    if not logs:
        text = "📜 <b>Audit log</b>\n\nNo actions logged yet."
    else:
        lines = ["📜 <b>Audit log</b> (last 15)\n"]
        for log in logs:
            lines.append(f"{log.created_at.strftime('%m-%d %H:%M')} — {log.action}: {log.detail}")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="admin:home")]])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# ---- Referral claims ----

@router.callback_query(F.data == "admin:claims")
async def admin_claims(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    await callback.answer()

    async with async_session() as session:
        result = await session.execute(
            select(ClaimRequest, User)
            .join(User, ClaimRequest.user_id == User.id)
            .where(ClaimRequest.status == "pending")
            .order_by(ClaimRequest.created_at.asc())
            .limit(10)
        )
        rows = result.all()

    if not rows:
        text = "💸 <b>Pending claims</b>\n\nNothing waiting right now."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="admin:home")]])
        await callback.message.edit_text(text, reply_markup=kb)
        return

    lines = ["💸 <b>Pending claims</b>\n"]
    btn_rows = []
    for claim, user in rows:
        tag = f"@{user.username}" if user.username else str(user.telegram_id)
        lines.append(f"#{claim.id} — {float(claim.amount_ton):g} TON — {tag}")
        btn_rows.append([
            InlineKeyboardButton(text=f"✅ #{claim.id}", callback_data=f"adm:claim:approve:{claim.id}"),
            InlineKeyboardButton(text=f"❌ #{claim.id}", callback_data=f"adm:claim:decline:{claim.id}"),
        ])
    btn_rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="admin:home")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=btn_rows))


@router.callback_query(F.data.startswith("adm:claim:approve:"))
async def admin_approve_claim(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    claim_id = int(callback.data.rsplit(":", 1)[1])

    async with async_session() as session:
        claim = await session.get(ClaimRequest, claim_id)
        if claim is None or claim.status != "pending":
            await callback.answer("This claim was already resolved.", show_alert=True)
            return
        user = await session.get(User, claim.user_id)

        # deduct now so the same balance can't be claimed twice, even if payout happens later
        if float(user.referral_balance_ton) + 1e-9 < float(claim.amount_ton):
            await callback.answer("User's balance no longer covers this claim — decline it instead.", show_alert=True)
            return
        user.referral_balance_ton = float(user.referral_balance_ton) - float(claim.amount_ton)
        user.referral_claimed_total_ton = float(user.referral_claimed_total_ton) + float(claim.amount_ton)
        claim.status = "approved"
        claim.admin_id = callback.from_user.id
        claim.resolved_at = datetime.utcnow()
        await session.commit()
        amount, payout_address, trader_tg_id = float(claim.amount_ton), claim.payout_address, user.telegram_id

    await _log_action(callback.from_user.id, "approve_claim", f"claim #{claim_id}: {amount:g} TON to {payout_address}")
    await callback.answer("Approved")
    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ <b>Approved</b> by admin. Send {amount:g} TON to:\n<code>{payout_address}</code>",
    )
    try:
        await callback.bot.send_message(
            trader_tg_id,
            f"✅ <b>Your claim was approved!</b>\n\n{amount:g} TON is being sent to your wallet.",
        )
    except Exception as e:
        logger.warning(f"could not notify user {trader_tg_id} of claim approval: {e}")


@router.callback_query(F.data.startswith("adm:claim:decline:"))
async def admin_decline_claim(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer()
    claim_id = int(callback.data.rsplit(":", 1)[1])

    async with async_session() as session:
        claim = await session.get(ClaimRequest, claim_id)
        if claim is None or claim.status != "pending":
            await callback.answer("This claim was already resolved.", show_alert=True)
            return

    await callback.answer()
    await state.set_state(AdminEdit.waiting_for_decline_reason)
    await state.update_data(decline_claim_id=claim_id)
    await callback.message.answer(f"Reason for declining claim #{claim_id}? (sent to the user). Send /cancel to abort.")


@router.message(AdminEdit.waiting_for_decline_reason)
async def admin_decline_reason(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    claim_id = data.get("decline_claim_id")
    reason = message.text.strip()
    await state.clear()

    async with async_session() as session:
        claim = await session.get(ClaimRequest, claim_id) if claim_id else None
        if claim is None or claim.status != "pending":
            await message.answer("That claim was already resolved.")
            return
        claim.status = "declined"
        claim.admin_id = message.from_user.id
        claim.admin_note = reason[:500]
        claim.resolved_at = datetime.utcnow()
        user = await session.get(User, claim.user_id)
        await session.commit()
        amount, trader_tg_id = float(claim.amount_ton), user.telegram_id

    await _log_action(message.from_user.id, "decline_claim", f"claim #{claim_id}: {reason[:200]}")
    await message.answer(f"❌ Claim #{claim_id} declined. The balance was left untouched.")
    try:
        await message.bot.send_message(
            trader_tg_id,
            f"❌ <b>Your referral claim was declined.</b>\n\n"
            f"Amount: {amount:g} TON\nReason: {esc(reason)}\n\n"
            "Your balance was not deducted — you can submit a new claim from 🎁 Referral.",
        )
    except Exception as e:
        logger.warning(f"could not notify user {trader_tg_id} of claim decline: {e}")
