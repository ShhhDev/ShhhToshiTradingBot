"""
Background poller that watches every user's wallet for incoming deposits
(native TON or any jetton) and DMs them when new funds land, with inline
Buy/Sell buttons on the notification.

This is polling-based (simple, works with tonapi.io reads you already have
in ton_client.py) rather than webhook/websocket-based. For a small number
of users this is fine; at scale, swap this loop for tonapi.io's webhook or
websocket subscription API and drop the polling interval entirely.
"""

import asyncio
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from db import async_session, Wallet, User, DepositSnapshot
import ton_client

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30
TON_ASSET_KEY = "TON"


async def _get_snapshot(session, wallet_id: int, asset: str) -> DepositSnapshot | None:
    result = await session.execute(
        select(DepositSnapshot).where(
            DepositSnapshot.wallet_id == wallet_id,
            DepositSnapshot.asset == asset,
        )
    )
    return result.scalar_one_or_none()


def _deposit_kb(asset: str, symbol: str) -> InlineKeyboardMarkup:
    if asset == TON_ASSET_KEY:
        # TON itself isn't "bought/sold" against itself — offer buy of other
        # tokens with it, and a link back to balance.
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Buy a token with this TON", callback_data="menu:buy")],
            [InlineKeyboardButton(text="💰 View Balance", callback_data="menu:balance")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"🟢 Buy more {symbol}", callback_data=f"buy:holding:{asset}"),
            InlineKeyboardButton(text=f"🔴 Sell {symbol}", callback_data=f"sell:holding:{asset}"),
        ],
        [InlineKeyboardButton(text="💰 View Balance", callback_data="menu:balance")],
    ])


async def _check_wallet(bot: Bot, session, wallet: Wallet, telegram_id: int):
    # --- native TON ---
    try:
        ton_balance = await ton_client.get_ton_balance(wallet.address)
    except Exception as e:
        logger.warning(f"deposit_watcher: TON balance read failed for {wallet.address}: {e}")
        ton_balance = None

    if ton_balance is not None:
        snap = await _get_snapshot(session, wallet.id, TON_ASSET_KEY)
        if snap is None:
            session.add(DepositSnapshot(
                wallet_id=wallet.id, asset=TON_ASSET_KEY, symbol="TON",
                last_balance=ton_balance,
            ))
            await session.commit()
        elif ton_balance > float(snap.last_balance) + 1e-9:
            delta = ton_balance - float(snap.last_balance)
            snap.last_balance = ton_balance
            await session.commit()
            await _notify_deposit(bot, telegram_id, delta, "TON", TON_ASSET_KEY)

    # --- jettons ---
    try:
        holdings = await ton_client.get_jetton_holdings(wallet.address)
    except Exception as e:
        logger.warning(f"deposit_watcher: jetton read failed for {wallet.address}: {e}")
        return

    for h in holdings:
        asset = h["contract"]
        if not asset:
            continue
        snap = await _get_snapshot(session, wallet.id, asset)
        if snap is None:
            session.add(DepositSnapshot(
                wallet_id=wallet.id, asset=asset, symbol=h["symbol"],
                last_balance=h["balance"],
            ))
            await session.commit()
            # First time we've ever seen this wallet hold this jetton at all —
            # treat the whole balance as a fresh deposit (don't stay silent
            # on the user's very first token deposit just because there was
            # no prior snapshot row).
            if h["balance"] > 0:
                await _notify_deposit(bot, telegram_id, h["balance"], h["symbol"], asset)
            continue

        if h["balance"] > float(snap.last_balance) + 1e-9:
            delta = h["balance"] - float(snap.last_balance)
            snap.last_balance = h["balance"]
            snap.symbol = h["symbol"]
            await session.commit()
            await _notify_deposit(bot, telegram_id, delta, h["symbol"], asset)


async def _notify_deposit(bot: Bot, telegram_id: int, amount: float, symbol: str, asset: str):
    text = (
        f"🎉 <b>Deposit received</b>\n\n"
        f"+{amount:.6f} <b>{symbol}</b> just landed in your wallet."
    )
    try:
        await bot.send_message(telegram_id, text, reply_markup=_deposit_kb(asset, symbol))
    except Exception as e:
        logger.warning(f"deposit_watcher: failed to notify {telegram_id}: {e}")


async def run_deposit_watcher(bot: Bot):
    """Long-running loop — schedule with asyncio.create_task() from main.py."""
    logger.info("Deposit watcher started.")
    while True:
        try:
            async with async_session() as session:
                result = await session.execute(select(Wallet, User).join(User, Wallet.user_id == User.id))
                rows = result.all()
                for wallet, user in rows:
                    if user.is_banned:
                        continue
                    await _check_wallet(bot, session, wallet, user.telegram_id)
        except Exception as e:
            logger.exception(f"deposit_watcher: loop error: {e}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
