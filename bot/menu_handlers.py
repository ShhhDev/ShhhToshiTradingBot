"""
Taps on the persistent bottom menu (💰 Balance / 🔁 Swap / ⚙️ Settings / 📥 Deposit / 🎁 Referral).

This router is registered FIRST in main.py so a menu tap always wins, even if the
user is in the middle of typing an amount or pasting an address: the half-finished
flow is dropped and the tapped screen opens.
"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import auto_handlers
import dashboard
import balance_handlers
import deposit_handlers
import market_handlers
import referral_handlers
import settings_handlers
import trade_handlers
import transfer_handlers
from helpers import (
    MENU_HOME, MENU_BALANCE, MENU_SWAP, MENU_SETTINGS, MENU_DEPOSIT, MENU_REFERRAL,
    MENU_POSITIONS, MENU_TRANSFER, MENU_EXPLORE, MENU_COPY, MENU_SNIPES, MENU_LIMIT,
)

router = Router()


@router.message(F.text == MENU_HOME)
async def menu_home(message: Message, state: FSMContext):
    await state.clear()
    await dashboard.send_dashboard(message)


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


@router.message(F.text == MENU_REFERRAL)
async def menu_referral(message: Message, state: FSMContext):
    await state.clear()
    await referral_handlers.send_referral(message)


@router.message(F.text == MENU_POSITIONS)
async def menu_positions(message: Message, state: FSMContext):
    await state.clear()
    await market_handlers.send_positions(message)


@router.message(F.text == MENU_TRANSFER)
async def menu_transfer(message: Message, state: FSMContext):
    await transfer_handlers.start_transfer(message, state)


@router.message(F.text == MENU_EXPLORE)
async def menu_explore(message: Message, state: FSMContext):
    await state.clear()
    await market_handlers.send_explore(message)


@router.message(F.text == MENU_COPY)
async def menu_copy(message: Message, state: FSMContext):
    await state.clear()
    await auto_handlers.send_copy_trades(message)


@router.message(F.text == MENU_SNIPES)
async def menu_snipes(message: Message, state: FSMContext):
    await state.clear()
    await auto_handlers.send_snipes(message)


@router.message(F.text == MENU_LIMIT)
async def menu_limit(message: Message, state: FSMContext):
    await state.clear()
    await auto_handlers.send_limit_orders(message)
