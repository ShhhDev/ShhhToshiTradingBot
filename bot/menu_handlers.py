"""
Taps on the persistent bottom menu (💰 Balance / 🔁 Swap / ⚙️ Settings / 📥 Deposit).

This router is registered FIRST in main.py so a menu tap always wins, even if the
user is in the middle of typing an amount or pasting an address: the half-finished
flow is dropped and the tapped screen opens.
"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import balance_handlers
import deposit_handlers
import settings_handlers
import trade_handlers
from helpers import MENU_BALANCE, MENU_SWAP, MENU_SETTINGS, MENU_DEPOSIT

router = Router()


@router.message(F.text == MENU_BALANCE)
async def menu_balance(message: Message, state: FSMContext):
    await state.clear()
    await balance_handlers.send_balance(message)


@router.message(F.text == MENU_SWAP)
async def menu_swap(message: Message, state: FSMContext):
    await state.clear()
    await trade_handlers.start_swap(message, state)


@router.message(F.text == MENU_SETTINGS)
async def menu_settings(message: Message, state: FSMContext):
    await state.clear()
    await settings_handlers.send_settings(message)


@router.message(F.text == MENU_DEPOSIT)
async def menu_deposit(message: Message, state: FSMContext):
    await state.clear()
    await deposit_handlers.send_deposit(message)
