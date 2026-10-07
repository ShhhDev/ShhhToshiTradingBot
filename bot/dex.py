"""
DEX integration — STON.fi swaps (which also routes through the pools STON.fi indexes).

How it works (follows docs.ston.fi, "Swap (v2)"):
  1. swap_builder/build_swap.mjs asks api.ston.fi to simulate the swap, then uses the OFFICIAL
     @ston-fi/sdk to build the unsigned router message (to / value / body).
  2. This module signs that message with the user's wallet (tonsdk) and broadcasts it via tonapi.
The private key never leaves Python. STON.fi's API is MAINNET ONLY, so there is no testnet path:
test with a tiny amount (e.g. 0.2 TON) before enabling DEX_LIVE for everyone.

Limits worth knowing: a swap is "sent", not "confirmed" - the tx hash returned is the message
hash, and if the pool rejects it (slippage) the router refunds on-chain. DeDust-only pools that
STON.fi doesn't route are not covered.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal

import ton_client
from config import config

logger = logging.getLogger(__name__)

# Off by default. Set DEX_LIVE=true in the environment after a successful tiny test swap.
LIVE = config.DEX_LIVE

_BUILDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "swap_builder", "build_swap.mjs")
TON = "TON"


class DexError(Exception):
    """A readable reason a quote or swap couldn't be produced."""


@dataclass
class Quote:
    token_in: str
    token_out: str
    amount_in: float
    amount_out_estimated: float
    price_impact_pct: float
    min_amount_out: float  # after slippage
    route: str
    offer_units: int = 0
    slippage_bps: int = 100


def _asset(token: str) -> str:
    return "ton" if token == TON else token


async def _decimals(token: str) -> int:
    if token == TON:
        return 9
    meta = await ton_client.get_token_metadata(token)
    if meta is None:
        raise DexError("Couldn't load that token's details.")
    return int(meta["decimals"])


async def _run_builder(payload: dict) -> dict:
    if not os.path.exists(_BUILDER):
        raise DexError("Swap helper is missing (bot/swap_builder).")
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", _BUILDER,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=os.path.dirname(_BUILDER),
        )
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), timeout=45)
    except FileNotFoundError as e:
        raise DexError("Node.js isn't installed on the server (needed for swaps).") from e
    except asyncio.TimeoutError as e:
        raise DexError("The swap service timed out. Please try again.") from e
    if proc.returncode != 0:
        try:
            msg = json.loads(err.decode() or "{}").get("error", "")
        except ValueError:
            msg = err.decode()[:200]
        logger.error("swap builder failed: %s", msg)
        raise DexError(f"Couldn't route this swap ({msg[:120] or 'unknown error'}). The token may have no liquidity.")
    try:
        return json.loads(out.decode())
    except ValueError as e:
        raise DexError("The swap service returned something unreadable.") from e


async def get_quote(token_in: str, token_out: str, amount_in: float, slippage_bps: int) -> Quote:
    dec_in, dec_out = await _decimals(token_in), await _decimals(token_out)
    units = int(Decimal(str(amount_in)) * (10 ** dec_in))  # floors; never rounds up past the balance
    if units <= 0:
        raise DexError("That amount is too small to swap.")
    res = await _run_builder({
        "mode": "quote", "offer": _asset(token_in), "ask": _asset(token_out),
        "units": str(units), "slippage": f"{slippage_bps / 10_000:.4f}",
    })
    ask = int(res["ask_units"]) / 10 ** dec_out
    min_ask = int(res["min_ask_units"]) / 10 ** dec_out
    if ask <= 0:
        raise DexError("No liquidity found for this pair.")
    try:
        impact = float(res.get("price_impact") or 0) * 100
    except (TypeError, ValueError):
        impact = 0.0
    return Quote(token_in, token_out, amount_in, ask, impact, min_ask, "STON.fi", units, slippage_bps)


async def execute_swap(wallet_mnemonic: str, quote: Quote) -> str:
    """Builds the router message with the official SDK, signs it here, broadcasts it.
    Returns the message hash once the wallet's seqno has advanced (message accepted)."""
    wallet = ton_client._wallet_from_mnemonic(wallet_mnemonic)
    address = wallet.address.to_string(is_user_friendly=True, is_bounceable=False)
    res = await _run_builder({
        "mode": "build", "offer": _asset(quote.token_in), "ask": _asset(quote.token_out),
        "units": str(quote.offer_units), "slippage": f"{quote.slippage_bps / 10_000:.4f}", "wallet": address,
    })
    try:
        return await ton_client.send_raw(wallet_mnemonic, res["to"], int(res["value"]), res.get("body"))
    except ton_client.TonApiError as e:
        raise DexError(f"The swap couldn't be sent: {e}") from e
