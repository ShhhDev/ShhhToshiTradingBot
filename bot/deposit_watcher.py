"""
Deposit detection.

detect_new_deposits() compares a wallet's live balances against the last-seen
snapshot (DepositSnapshot table) and returns whatever newly arrived. It is used
in two places:
  * run_deposit_watcher() - background loop that DMs users when funds land
  * the "Check Deposit" button (deposit_handlers.py) - on-demand check

Baselines follow the balance DOWN as well as up (gas, trades, withdrawals), so
a later top-up is always measured against what the wallet actually held.

This is polling-based (simple, uses the tonapi reads already in ton_client.py).
At scale, swap the loop for tonapi.io webhooks/websockets.
"""

import asyncio
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

import addr_utils
import ton_client
from db import async_session, Wallet, User, DepositSnapshot
from helpers import esc, fmt_amount

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30
TON_ASSET_KEY = "TON"
EPS = 1e-9


async def _get_snapshot(session, wallet_id: int, asset: str) -> DepositSnapshot | None:
    result = await session.execute(
        select(DepositSnapshot).where(
            DepositSnapshot.wallet_id == wallet_id,
            DepositSnapshot.asset == asset,
        )
    )
    return result.scalars().first()


def deposit_kb(asset: str, symbol: str) -> InlineKeyboardMarkup:
    rows = []
    if asset == TON_ASSET_KEY:
        rows.append([InlineKeyboardButton(text="🔁 Swap TON for a token", callback_data="swap:start")])
    else:
        friendly = addr_utils.to_friendly(asset)
        if friendly:
            rows.append([InlineKeyboardButton(text=f"🪙 Open {symbol[:20]}", callback_data=f"tk:{friendly}")])
    rows.append([InlineKeyboardButton(text="💰 View Balance", callback_data="bal:refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def detect_new_deposits(session, wallet: Wallet, strict: bool = False) -> list[dict]:
    """
    Returns [{"amount": float, "symbol": str, "asset": str}] for everything that
    arrived since the last snapshot, and moves the snapshots forward.
    strict=False (background loop): provider errors are logged and skipped.
    strict=True  (user pressed Check Deposit): provider errors are raised so the
    user is told instead of being shown a misleading "nothing arrived".
    """
    found: list[dict] = []

    # --- native TON ---
    try:
        ton_balance = await ton_client.get_ton_balance(wallet.address)
    except Exception as e:
        if strict:
            raise
        logger.warning(f"deposit_watcher: TON balance read failed for {wallet.address}: {e}")
        ton_balance = None

    if ton_balance is not None:
        snap = await _get_snapshot(session, wallet.id, TON_ASSET_KEY)
        if snap is None:
            session.add(DepositSnapshot(
                wallet_id=wallet.id, asset=TON_ASSET_KEY, symbol="TON", last_balance=ton_balance,
            ))
            await session.commit()
        else:
            previous = float(snap.last_balance)
            if ton_balance > previous + EPS:
                snap.last_balance = ton_balance
                await session.commit()
                found.append({"amount": ton_balance - previous, "symbol": "TON", "asset": TON_ASSET_KEY})
            elif ton_balance < previous - EPS:
                snap.last_balance = ton_balance  # spent / gas: lower the baseline
                await session.commit()

    # --- jettons ---
    try:
        holdings = await ton_client.get_jetton_holdings(wallet.address)
    except Exception as e:
        if strict:
            raise
        logger.warning(f"deposit_watcher: jetton read failed for {wallet.address}: {e}")
        return found

    present = set()
    for h in holdings:
        asset = h["raw"]
        if not asset:
            continue
        present.add(asset)
        snap = await _get_snapshot(session, wallet.id, asset)
        if snap is None:
            session.add(DepositSnapshot(
                wallet_id=wallet.id, asset=asset, symbol=h["symbol"][:32], last_balance=h["balance"],
            ))
            await session.commit()
            # First time this wallet is seen holding this jetton: the whole balance is new.
            if h["balance"] > 0:
                found.append({"amount": h["balance"], "symbol": h["symbol"], "asset": asset})
            continue

        previous = float(snap.last_balance)
        if h["balance"] > previous + EPS:
            snap.last_balance = h["balance"]
            snap.symbol = h["symbol"][:32]
            await session.commit()
            found.append({"amount": h["balance"] - previous, "symbol": h["symbol"], "asset": asset})
        elif h["balance"] < previous - EPS:
            snap.last_balance = h["balance"]
            await session.commit()

    # tokens that left the wallet entirely: reset their baseline to 0 so buying
    # the same token again later is detected correctly
    result = await session.execute(
        select(DepositSnapshot).where(
            DepositSnapshot.wallet_id == wallet.id,
            DepositSnapshot.asset != TON_ASSET_KEY,
        )
    )
    changed = False
    for snap in result.scalars().all():
        if snap.asset not in present and float(snap.last_balance) > 0:
            snap.last_balance = 0
            changed = True
    if changed:
        await session.commit()

    return found


async def _notify_deposit(bot: Bot, telegram_id: int, amount: float, symbol: str, asset: str):
    text = (
        "🎉 <b>Deposit received</b>\n\n"
        f"+{fmt_amount(amount, 6)} <b>{esc(symbol)}</b> just landed in your wallet."
    )
    try:
        await bot.send_message(telegram_id, text, reply_markup=deposit_kb(asset, symbol))
    except Exception as e:
        logger.warning(f"deposit_watcher: failed to notify {telegram_id}: {e}")


async def _check_wallet(bot: Bot, session, wallet: Wallet, telegram_id: int):
    for dep in await detect_new_deposits(session, wallet, strict=False):
        await _notify_deposit(bot, telegram_id, dep["amount"], dep["symbol"], dep["asset"])


async def run_deposit_watcher(bot: Bot):
    """Long-running loop - schedule with asyncio.create_task() from main.py."""
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
