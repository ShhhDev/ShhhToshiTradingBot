"""
One pipeline for every fee-bearing action: swaps from the Swap menu, limit orders, copy trades
and snipes all go through run_swap(), so the admin-set fee (FeeConfig.fee_bps, default 5%)
applies to all of them in exactly one place.

NOT fee-bearing (by design): deposits and withdrawals/transfers (transfer_handlers.py).
"""

import logging

import dex
import encryption
import fees
import ton_client
from db import async_session, Trade, User, Wallet

logger = logging.getLogger(__name__)
TON = "TON"


class TradeError(Exception):
    """A readable reason a trade didn't happen."""


async def _fee_in_ton(token: str, amount: float) -> float:
    if token == TON:
        return amount
    prices = await ton_client.get_usd_prices([token])
    import addr_utils
    token_usd, ton_usd = prices.get(addr_utils.to_raw(token)), prices.get("TON")
    return amount * token_usd / ton_usd if token_usd and ton_usd else 0.0


async def run_swap(user: User, wallet: Wallet, token_in: str, token_out: str,
                   amount_in: float, slippage_bps: int | None = None) -> Trade:
    """Fee is taken from the input token; the rest is swapped. Raises TradeError / NotImplementedError."""
    if not dex.LIVE:
        raise NotImplementedError("DEX_LIVE is off")
    fc = await fees.get_fee_config()
    if not fc.trading_enabled:
        raise TradeError("Trading is paused right now.")

    fee_amount, net = fees.calculate_fee(amount_in, fc.fee_bps)
    mnemonic = encryption.decrypt_mnemonic(wallet.encrypted_mnemonic, wallet.wrapped_data_key)

    try:
        quote = await dex.get_quote(token_in, token_out, net, slippage_bps or user.slippage_bps)
        seqno_before = await ton_client.get_seqno(wallet.address)
        tx_hash = await dex.execute_swap(mnemonic, quote)
    except dex.DexError as e:
        raise TradeError(str(e)) from e

    fee_tx = None
    if fee_amount > 0 and fc.dev_wallet:
        try:
            await ton_client.wait_for_seqno(wallet.address, seqno_before)
            if token_in == TON:
                fee_tx = await ton_client.send_ton(mnemonic, fc.dev_wallet, fee_amount)
            else:
                fee_tx = await ton_client.send_jetton(mnemonic, wallet.address, token_in, fc.dev_wallet, fee_amount)
        except Exception as e:  # the swap already happened - never report it as failed
            logger.error("fee transfer failed after a successful swap (trade still recorded)", exc_info=e)

    fee_ton = await _fee_in_ton(token_in, fee_amount) if fee_tx else 0.0
    async with async_session() as session:
        trade = Trade(
            user_id=user.id, wallet_address=wallet.address,
            trade_type="buy" if token_in == TON else ("sell" if token_out == TON else "swap"),
            token_in=token_in, token_out=token_out,
            amount_in=amount_in, amount_out=quote.amount_out_estimated,
            fee_bps_applied=fc.fee_bps, fee_amount_ton=fee_ton, fee_tx_hash=fee_tx,
            tx_hash=tx_hash, status="success",
        )
        session.add(trade)
        await session.commit()
        await session.refresh(trade)

    if fee_tx and fee_ton > 0:
        from referral_handlers import award_referral_credit
        await award_referral_credit(fee_ton, user.telegram_id)
    return trade
