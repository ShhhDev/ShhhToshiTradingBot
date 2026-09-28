"""
Persistent bottom menu: 💰 Balance / 📥 Deposit / 🔁 Swap / ⚙️ Settings.

Matching is intentionally loose (ignores emoji variants) so older keyboards
and different phone emoji fonts still hit the right handler.
"""

import logging
import re

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import balance_handlers
import deposit_handlers
import settings_handlers
import trade_handlers

logger = logging.getLogger(__name__)
router = Router()


def _label(text: str | None) -> str:
    """Strip emoji / symbols → lowercase keyword (balance, deposit, swap, settings)."""
    if not text:
        return ""
    # Keep only letters and spaces
    cleaned = re.sub(r"[^A-Za-z\s]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned


def _is_balance(text: str | None) -> bool:
    return _label(text) in {"balance", "bal"}


def _is_deposit(text: str | None) -> bool:
    return _label(text) in {"deposit", "dep"}


def _is_swap(text: str | None) -> bool:
    return _label(text) in {"swap"}


def _is_settings(text: str | None) -> bool:
    return _label(text) in {"settings", "setting", "set"}


@router.message(F.text.func(_is_balance))
async def menu_balance(message: Message, state: FSMContext):
    logger.info("menu: Balance from user %s", message.from_user.id)
    await state.clear()
    await balance_handlers.send_balance(message)


@router.message(F.text.func(_is_deposit))
async def menu_deposit(message: Message, state: FSMContext):
    logger.info("menu: Deposit from user %s", message.from_user.id)
    await state.clear()
    await deposit_handlers.send_deposit(message)


@router.message(F.text.func(_is_swap))
async def menu_swap(message: Message, state: FSMContext):
    logger.info("menu: Swap from user %s", message.from_user.id)
    await state.clear()
    await trade_handlers.start_swap(message, state)


@router.message(F.text.func(_is_settings))
async def menu_settings(message: Message, state: FSMContext):
    logger.info("menu: Settings from user %s", message.from_user.id)
    await state.clear()
    await settings_handlers.send_settings(message)
