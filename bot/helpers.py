"""Shared helpers: persistent menu, wallet lookups, safe message editing, formatting."""

import asyncio
import html
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from sqlalchemy import select

from config import config
from db import async_session, User, Wallet

logger = logging.getLogger(__name__)

# ---- persistent bottom menu (reply keyboard) --------------------------------

MENU_BALANCE = "💰 Balance"
MENU_DEPOSIT = "📥 Deposit"
MENU_BUY = "🟢 Buy"
MENU_SELL = "🔴 Sell"
MENU_SWAP = "🔁 Swap"
MENU_SETTINGS = "⚙️ Settings"

# Accept common variants already sitting on users' reply keyboards
MENU_BALANCE_ALIASES = {MENU_BALANCE, "Balance", "💰Balance"}
MENU_DEPOSIT_ALIASES = {MENU_DEPOSIT, "Deposit", "📥Deposit"}
MENU_BUY_ALIASES = {MENU_BUY, "Buy", "🟢Buy", "🟢  Buy"}
MENU_SELL_ALIASES = {MENU_SELL, "Sell", "🔴Sell", "🔴  Sell"}
MENU_SWAP_ALIASES = {MENU_SWAP, "🔄 Swap", "Swap", "🔁Swap", "🔄Swap"}
MENU_SETTINGS_ALIASES = {MENU_SETTINGS, "Settings", "⚙️Settings"}

MENU_TEXTS = (
    MENU_BALANCE_ALIASES | MENU_DEPOSIT_ALIASES | MENU_BUY_ALIASES
    | MENU_SELL_ALIASES | MENU_SWAP_ALIASES | MENU_SETTINGS_ALIASES
)


def main_reply_kb() -> ReplyKeyboardMarkup:
    """Bottom menu — always visible. System keyboard only opens when the user
    taps the text field (to type an amount or paste a CA)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_BALANCE), KeyboardButton(text=MENU_DEPOSIT)],
            [KeyboardButton(text=MENU_BUY), KeyboardButton(text=MENU_SELL)],
            [KeyboardButton(text=MENU_SWAP)],
            [KeyboardButton(text=MENU_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Paste a token CA or type an amount…",
    )


def remove_reply_kb() -> ReplyKeyboardRemove:
    """Only used if we ever need to clear the menu (we normally don't)."""
    from aiogram.types import ReplyKeyboardRemove
    return ReplyKeyboardRemove(remove_keyboard=True)


def onboarding_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Create Wallet", callback_data="wallet:create")],
        [InlineKeyboardButton(text="📥 Import Wallet", callback_data="wallet:import")],
    ])


# ---- small utilities --------------------------------------------------------

def esc(value) -> str:
    """HTML-escape untrusted text (token names, error messages) for parse_mode=HTML."""
    return html.escape(str(value), quote=False)


def is_admin(telegram_id: int) -> bool:
    return telegram_id in config.ADMIN_TELEGRAM_IDS


_bg_tasks: set = set()


def spawn(coro):
    """create_task that keeps a reference so the task isn't garbage-collected mid-flight."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def short_addr(a: str) -> str:
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 10 else (a or "")


def fmt_pct_bps(bps) -> str:
    return f"{float(bps) / 100:g}%"


def fmt_amount(x, max_dec: int = 4) -> str:
    x = float(x)
    if x == 0:
        return "0"
    s = f"{x:,.{max_dec}f}" if abs(x) >= 1 else f"{x:.8f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("0", "-0"):
        return f"{x:.2e}"
    return s


def fmt_usd(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    if x >= 1:
        return f"${x:,.2f}"
    if x >= 0.01:
        return f"${x:.4f}"
    return f"${x:.6f}"


def fmt_big_usd(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= limit:
            return f"${x / limit:.2f}{suffix}"
    return f"${x:,.2f}"


def fmt_price(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    if x == 0:
        return "$0"
    if x >= 1:
        return f"${x:,.4f}"
    return "$" + f"{x:.10f}".rstrip("0")


# ---- users & wallets --------------------------------------------------------

async def get_or_create_user(tg_user) -> User:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == tg_user.id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=tg_user.id, username=tg_user.username)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


async def list_wallets(telegram_id: int) -> list[Wallet]:
    async with async_session() as session:
        result = await session.execute(
            select(Wallet)
            .join(User, Wallet.user_id == User.id)
            .where(User.telegram_id == telegram_id)
            .order_by(Wallet.id)
        )
        return list(result.scalars().all())


async def get_active_wallet(telegram_id: int) -> Wallet | None:
    wallets = await list_wallets(telegram_id)
    if not wallets:
        return None
    active = [w for w in wallets if w.is_primary]
    # tolerate legacy data where more than one wallet was flagged primary
    return active[-1] if active else wallets[0]


async def get_wallet_by_id(telegram_id: int, wallet_id: int) -> Wallet | None:
    """Ownership-checked lookup: a user can only ever load their own wallets."""
    for w in await list_wallets(telegram_id):
        if w.id == wallet_id:
            return w
    return None


async def set_active_wallet(telegram_id: int, wallet_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(Wallet)
            .join(User, Wallet.user_id == User.id)
            .where(User.telegram_id == telegram_id)
        )
        wallets = list(result.scalars().all())
        if not any(w.id == wallet_id for w in wallets):
            return False
        for w in wallets:
            w.is_primary = (w.id == wallet_id)
        await session.commit()
        return True


async def require_wallet(event) -> Wallet | None:
    """Returns the active wallet, or tells the user to create/import one and returns None."""
    wallet = await get_active_wallet(event.from_user.id)
    if wallet:
        return wallet
    text = "You don't have a wallet yet. Create a new one or import an existing wallet:"
    if isinstance(event, CallbackQuery):
        try:
            await event.answer()
        except Exception:
            pass  # already answered by the caller
        await event.message.answer(text, reply_markup=onboarding_kb())
    else:
        await event.answer(text, reply_markup=onboarding_kb())
    return None


# ---- messaging --------------------------------------------------------------

async def edit(message: Message, text: str, kb: InlineKeyboardMarkup | None = None):
    """edit_text that never crashes on 'message is not modified' and falls back to a new message."""
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        logger.warning(f"edit_text failed, sending a new message instead: {e}")
        await message.answer(text, reply_markup=kb)


async def reply_or_edit(event, text: str, kb: InlineKeyboardMarkup | None = None):
    if isinstance(event, CallbackQuery):
        await edit(event.message, text, kb)
    else:
        await event.answer(text, reply_markup=kb)
