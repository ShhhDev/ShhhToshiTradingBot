"""
Taps on the persistent bottom menu
(💰 Balance / 📥 Deposit / 🔁 Swap / ⚙️ Settings).

Buy & Sell are NOT on this keyboard — they appear as inline buttons
on token cards (and after pasting a CA).
"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import balance_handlers
import deposit_handlers
import settings_handlers
import trade_handlers
from helpers import (
    MENU_BALANCE_ALIASES, MENU_DEPOSIT_ALIASES,
    MENU_SWAP_ALIASES, MENU_SETTINGS_ALIASES,
)

router = Router()


@router.message(F.text.in_(MENU_BALANCE_ALIASES))
async def menu_balance(message: Message, state: FSMContext):
    await state.clear()
    await balance_handlers.send_balance(message)


@router.message(F.text.in_(MENU_DEPOSIT_ALIASES))
async def menu_deposit(message: Message, state: FSMContext):
    await state.clear()
    await deposit_handlers.send_deposit(message)


@router.message(F.text.in_(MENU_SWAP_ALIASES))
async def menu_swap(message: Message, state: FSMContext):
    await state.clear()
    await trade_handlers.start_swap(message, state)


@router.message(F.text.in_(MENU_SETTINGS_ALIASES))
async def menu_settings(message: Message, state: FSMContext):
    await state.clear()
    await settings_handlers.send_settings(message)
