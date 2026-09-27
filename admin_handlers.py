"""
Admin controls, live inside the bot — no separate website, no second
Railway service. Only Telegram user IDs listed in ADMIN_TELEGRAM_IDS
(.env) can open this menu; everyone else gets silently ignored so the
bot doesn't even reveal that /admin exists.
"""

from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, func

from config import config
from db import async_session, FeeConfig, User, Wallet, Trade, AdminAuditLog

router = Router()


def is_admin(telegram_id: int) -> bool:
    return telegram_id in config.ADMIN_TELEGRAM_IDS


class AdminEdit(StatesGroup):
    waiting_for_fee_pct = State()
    waiting_for_dev_wallet = State()
    waiting_for_max_trade = State()
    waiting_for_max_daily = State()
    waiting_for_confirm_threshold = State()


async def _log_action(admin_id: int, action: str, detail: str):
    async with async_session() as session:
        session.add(AdminAuditLog(admin_telegram_id=admin_id, action=action, detail=detail))
        await session.commit()


def admin_menu_kb(fc: FeeConfig) -> InlineKeyboardMarkup:
    status = "🟢 LIVE" if fc.trading_enabled else "🔴 PAUSED"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Fee: {fc.fee_bps/100:.1f}% (tap to change)", callback_data="admin:edit_fee")],
        [InlineKeyboardButton(text="Dev wallet (tap to change)", callback_data="admin:edit_wallet")],
        [InlineKeyboardButton(text=f"Max trade: {fc.max_trade_ton} TON", callback_data="admin:edit_max_trade")],
        [InlineKeyboardButton(text=f"Max daily/user: {fc.max_daily_volume_ton} TON", callback_data="admin:edit_max_daily")],
        [InlineKeyboardButton(text=f"Confirm threshold: {fc.large_trade_confirm_threshold_ton} TON", callback_data="admin:edit_threshold")],
        [InlineKeyboardButton(text=f"Trading: {status} (tap to toggle)", callback_data="admin:toggle_trading")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="admin:stats")],
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

async def _generic_numeric_setting(message: Message, state: FSMContext, field: str, label: str, admin_menu_kb_):
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
    await message.answer(f"✅ {label} updated to {value} TON.", reply_markup=admin_menu_kb_(fc))


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
