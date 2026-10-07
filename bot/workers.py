"""
Background loops that fire limit orders, copy trades and snipes. Every fill goes through
executor.run_swap(), so the admin-set fee applies. While dex.LIVE is False nothing fires.
"""

import asyncio
import logging
import time
from datetime import datetime

from sqlalchemy import select, update

import addr_utils
import dex
import executor
import ton_client
from config import config
from db import async_session, LimitOrder, CopyTrade, Snipe, User, Wallet
from helpers import esc, fmt_amount

logger = logging.getLogger(__name__)
TON = "TON"


async def _load(user_id: int, wallet_id: int):
    async with async_session() as s:
        return await s.get(User, user_id), await s.get(Wallet, wallet_id)


async def _notify(bot, user: User, text: str):
    try:
        await bot.send_message(user.telegram_id, text)
    except Exception:
        logger.warning("couldn't notify user %s", user.telegram_id)


async def _token_balance(address: str, token: str) -> float:
    for h in await ton_client.get_jetton_holdings(address):
        if addr_utils.same(h["contract"], token):
            return h["balance"]
    return 0.0


async def _buy(bot, user, wallet, token: str, amount_ton: float, slippage_bps: int | None, label: str) -> bool:
    """Returns True if a swap was attempted to completion (success), False otherwise."""
    bal = await ton_client.get_ton_balance(wallet.address)
    if bal < amount_ton + config.GAS_RESERVE_TON:
        await _notify(bot, user, f"❌ {label}: not enough TON ({fmt_amount(bal)}). Needed {fmt_amount(amount_ton + config.GAS_RESERVE_TON)} incl. gas reserve.")
        return False
    trade = await executor.run_swap(user, wallet, TON, token, amount_ton, slippage_bps)
    await _notify(bot, user, f"✅ {label}\nSpent {fmt_amount(amount_ton)} TON · tx <code>{esc(trade.tx_hash or '—')}</code>")
    return True


async def _sell_all(bot, user, wallet, token: str, label: str) -> bool:
    held = await _token_balance(wallet.address, token)
    if held <= 0:
        return False
    if await ton_client.get_ton_balance(wallet.address) < config.GAS_RESERVE_TON:
        await _notify(bot, user, f"❌ {label}: not enough TON for network fees.")
        return False
    trade = await executor.run_swap(user, wallet, token, TON, held)
    await _notify(bot, user, f"✅ {label}\nSold {fmt_amount(held)} · tx <code>{esc(trade.tx_hash or '—')}</code>")
    return True


# ------------------------------------------------------------------ limit orders

async def _limit_tick(bot):
    async with async_session() as s:
        orders = (await s.execute(select(LimitOrder).where(LimitOrder.status == "open"))).scalars().all()
    if not orders:
        return
    prices = await ton_client.get_usd_prices(list({o.token for o in orders}))
    for o in orders:
        price = prices.get(addr_utils.to_raw(o.token))
        trigger = float(o.trigger_price_usd)
        if not price or not ((o.side == "buy" and price <= trigger) or (o.side == "sell" and price >= trigger)):
            continue
        async with async_session() as s:  # claim it so it can never fill twice
            claimed = (await s.execute(update(LimitOrder).where(LimitOrder.id == o.id, LimitOrder.status == "open")
                                       .values(status="filling"))).rowcount
            await s.commit()
        if not claimed:
            continue
        user, wallet = await _load(o.user_id, o.wallet_id)
        status, note = "filled", None
        try:
            label = f"Limit order #{o.id} ({o.side} {o.symbol})"
            if o.side == "buy":
                ok = await _buy(bot, user, wallet, o.token, float(o.amount), None, label)
            else:
                held = await _token_balance(wallet.address, o.token)
                amt = min(float(o.amount), held)
                ok = amt > 0
                if ok:
                    trade = await executor.run_swap(user, wallet, o.token, TON, amt)
                    await _notify(bot, user, f"✅ {label}\nSold {fmt_amount(amt)} · tx <code>{esc(trade.tx_hash or '—')}</code>")
                else:
                    await _notify(bot, user, f"❌ {label}: you no longer hold this token.")
            if not ok:
                status, note = "failed", "insufficient balance"
        except Exception as e:
            logger.error("limit order %s failed", o.id, exc_info=e)
            status, note = "failed", str(e)[:200]
            await _notify(bot, user, f"❌ Limit order #{o.id} couldn't be filled. Nothing was charged for a failed swap.")
        async with async_session() as s:
            await s.execute(update(LimitOrder).where(LimitOrder.id == o.id)
                            .values(status=status, note=note, filled_at=datetime.utcnow()))
            await s.commit()


# ------------------------------------------------------------------ copy trade

def _swaps_by(event: dict, trader: str):
    """Yield (side, jetton_master, ton_amount) for swaps the trader made in this event."""
    for a in event.get("actions", []):
        if a.get("type") != "JettonSwap" or a.get("status") != "ok":
            continue
        sw = a.get("JettonSwap") or {}
        if not addr_utils.same((sw.get("user_wallet") or {}).get("address", ""), trader):
            continue
        ton_in, ton_out = int(sw.get("ton_in") or 0), int(sw.get("ton_out") or 0)
        out_m = (sw.get("jetton_master_out") or {}).get("address")
        in_m = (sw.get("jetton_master_in") or {}).get("address")
        if ton_in > 0 and out_m:
            yield "buy", addr_utils.canonical(out_m), ton_in / 1e9
        elif ton_out > 0 and in_m:
            yield "sell", addr_utils.canonical(in_m), ton_out / 1e9


async def _copy_tick(bot):
    async with async_session() as s:
        cfgs = (await s.execute(select(CopyTrade).where(CopyTrade.active.is_(True)))).scalars().all()
    for c in cfgs:
        try:
            events = await ton_client.get_account_events(c.trader_address, 20)
        except Exception:
            continue
        fresh = sorted((e for e in events if int(e.get("timestamp", 0)) > c.last_ts), key=lambda e: e["timestamp"])
        if not fresh:
            continue
        async with async_session() as s:  # advance the cursor first: never copy the same trade twice
            await s.execute(update(CopyTrade).where(CopyTrade.id == c.id).values(last_ts=int(fresh[-1]["timestamp"])))
            await s.commit()
        user, wallet = await _load(c.user_id, c.wallet_id)
        for ev in fresh:
            for side, token, _ton in _swaps_by(ev, c.trader_address):
                label = f"Copy trade #{c.id} ({side})"
                try:
                    if side == "buy":
                        await _buy(bot, user, wallet, token, float(c.buy_amount_ton), None, label)
                    else:
                        await _sell_all(bot, user, wallet, token, label)
                except Exception as e:
                    logger.error("copy trade %s failed", c.id, exc_info=e)
                    await _notify(bot, user, f"❌ {label} couldn't be executed.")


# ------------------------------------------------------------------ snipes

async def _snipe_tick(bot):
    async with async_session() as s:
        snipes = (await s.execute(select(Snipe).where(Snipe.active.is_(True), Snipe.status == "watching"))).scalars().all()
    for sn in snipes:
        try:
            if sn.kind == "deployer":
                await _snipe_deployer(bot, sn)
            else:
                await _snipe_jetton(bot, sn)
        except Exception as e:
            logger.error("snipe %s failed", sn.id, exc_info=e)


async def _snipe_deployer(bot, sn: Snipe):
    events = await ton_client.get_account_events(sn.target, 20)
    fresh = sorted((e for e in events if int(e.get("timestamp", 0)) > sn.last_ts), key=lambda e: e["timestamp"])
    if not fresh:
        return
    async with async_session() as s:
        await s.execute(update(Snipe).where(Snipe.id == sn.id).values(last_ts=int(fresh[-1]["timestamp"])))
        await s.commit()
    for ev in fresh:
        for a in ev.get("actions", []):
            addr = ((a.get("ContractDeploy") or {}).get("address"))
            if a.get("type") == "ContractDeploy" and addr and await ton_client.get_token_metadata(addr):
                async with async_session() as s:  # deployer snipe becomes a jetton snipe on the new token
                    await s.execute(update(Snipe).where(Snipe.id == sn.id).values(kind="jetton", target=addr_utils.canonical(addr)))
                    await s.commit()
                user, _ = await _load(sn.user_id, sn.wallet_id)
                await _notify(bot, user, f"🎯 Snipe #{sn.id}: deployer launched a new token — now waiting for liquidity.")
                return


async def _snipe_jetton(bot, sn: Snipe):
    market = await ton_client.get_token_market_data(sn.target)
    if not market or not market.get("liquidity_usd"):
        return
    allowed = [d for d in sn.dexes.split(",") if d]
    if allowed and not any(a in (market.get("dex") or "").lower().replace(".", "").replace(" ", "") for a in allowed):
        return
    async with async_session() as s:
        claimed = (await s.execute(update(Snipe).where(Snipe.id == sn.id, Snipe.status == "watching")
                                   .values(status="done", active=False))).rowcount
        await s.commit()
    if not claimed:
        return
    user, wallet = await _load(sn.user_id, sn.wallet_id)
    try:
        await _buy(bot, user, wallet, sn.target, float(sn.amount_ton), sn.slippage_bps, f"Snipe #{sn.id} (${market['symbol']})")
    except Exception as e:
        logger.error("snipe %s buy failed", sn.id, exc_info=e)
        async with async_session() as s:
            await s.execute(update(Snipe).where(Snipe.id == sn.id).values(status="failed", note=str(e)[:200]))
            await s.commit()
        await _notify(bot, user, f"❌ Snipe #{sn.id} couldn't be executed.")


# ------------------------------------------------------------------ runner

async def _loop(name: str, tick, bot, every: int):
    while True:
        try:
            if dex.LIVE:
                await tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("%s worker error", name, exc_info=e)
        await asyncio.sleep(every)


def start_workers(bot) -> list[asyncio.Task]:
    return [
        asyncio.create_task(_loop("limit", _limit_tick, bot, 20)),
        asyncio.create_task(_loop("copy", _copy_tick, bot, 15)),
        asyncio.create_task(_loop("snipe", _snipe_tick, bot, 8)),
    ]
