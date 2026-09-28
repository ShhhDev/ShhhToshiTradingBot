"""
DEX integration — STON.fi quote + on-chain swap execution.

get_quote()  → live against https://api.ston.fi
execute_swap() → builds STON.fi v1/v2-compatible messages with tonsdk,
                 signs with the user mnemonic, broadcasts via toncenter/tonapi.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx
from tonsdk.boc import Builder, Cell
from tonsdk.contract.token.ft import JettonWallet
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
from tonsdk.utils import Address, to_nano, bytes_to_b64str

import addr_utils
from config import config

logger = logging.getLogger(__name__)

_TON_ASSET = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"

# STON.fi v1 opcodes / known contracts (mainnet defaults; simulate may return others)
OP_JETTON_TRANSFER = 0x0F8A7EA5
OP_STONFI_SWAP_V1 = 0x25938561
OP_PTON_TON_TRANSFER = 0x01F3835D

# Gas attachments (nanoton) — conservative, excess is refunded
GAS_TON_TO_JETTON_FWD = to_nano(0.2, "ton")
GAS_JETTON_TO_JETTON = to_nano(0.3, "ton")
GAS_JETTON_TO_JETTON_FWD = to_nano(0.25, "ton")
GAS_JETTON_TO_TON = to_nano(0.25, "ton")
GAS_JETTON_TO_TON_FWD = to_nano(0.2, "ton")


@dataclass
class Quote:
    token_in: str
    token_out: str
    amount_in: float
    amount_out_estimated: float
    price_impact_pct: float
    min_amount_out: float
    route: str
    offer_units: str = ""
    min_ask_units: str = ""
    router_address: str = ""
    pool_address: str = ""
    ask_units: str = ""
    # extra fields from simulate needed to build the tx
    offer_jetton_wallet: str = ""
    ask_jetton_wallet: str = ""
    pton_wallet_address: str = ""
    pton_master_address: str = ""
    router_major_version: int = 1
    raw: dict = field(default_factory=dict, repr=False)


def _asset_for_api(token: str) -> str:
    if token == "TON" or not token:
        return _TON_ASSET
    friendly = addr_utils.to_friendly(token)
    if not friendly:
        raise ValueError(f"Invalid token address for DEX: {token}")
    return friendly


def _units_from_float(amount: float, decimals: int = 9) -> str:
    if amount <= 0:
        raise ValueError("amount must be positive")
    scale = 10 ** decimals
    units = int(amount * scale + 1e-12)
    if units <= 0:
        raise ValueError("amount too small for token decimals")
    return str(units)


def _float_from_units(units: str | int, decimals: int = 9) -> float:
    return int(units) / (10 ** decimals)


def _addr(s: str) -> Address:
    return Address(s)


async def get_quote(
    token_in: str,
    token_out: str,
    amount_in: float,
    slippage_bps: int,
    decimals_in: int = 9,
    decimals_out: int = 9,
) -> Quote:
    if amount_in <= 0:
        raise ValueError("Swap amount must be greater than zero")

    offer = _asset_for_api(token_in)
    ask = _asset_for_api(token_out)
    if offer == ask:
        raise ValueError("Cannot swap a token for itself")

    slip = max(int(slippage_bps), 1) / 10_000.0
    units = _units_from_float(amount_in, decimals_in)

    params = {
        "offer_address": offer,
        "ask_address": ask,
        "units": units,
        "slippage_tolerance": f"{slip:.6f}".rstrip("0").rstrip("."),
    }

    base = (config.STONFI_API_URL or "https://api.ston.fi").rstrip("/")
    url = f"{base}/v1/swap/simulate"

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.post(url, params=params)
        except httpx.HTTPError as e:
            logger.error("STON.fi simulate network error: %s", e)
            raise RuntimeError("DEX quote service unreachable. Try again in a few seconds.") from e

    if r.status_code == 400:
        detail = ""
        try:
            detail = (r.json() or {}).get("error") or r.text[:200]
        except Exception:
            detail = r.text[:200]
        raise RuntimeError(f"No route / insufficient liquidity: {detail or 'bad pair'}")

    if r.status_code >= 500:
        raise RuntimeError("DEX quote service is temporarily unavailable")

    try:
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error("STON.fi simulate bad response: %s %s", r.status_code, r.text[:300])
        raise RuntimeError("DEX returned an invalid quote response") from e

    ask_units = data.get("ask_units") or data.get("recommended_min_ask_units") or "0"
    min_ask = data.get("min_ask_units") or data.get("recommended_min_ask_units") or "0"
    impact = float(data.get("price_impact") or 0) * 100

    router = data.get("router") or {}
    router_addr = data.get("router_address") or router.get("address") or ""
    pool = data.get("pool_address") or ""
    major = int(router.get("major_version") or 1)
    route = f"STON.fi v{major}"

    return Quote(
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_in,
        amount_out_estimated=_float_from_units(ask_units, decimals_out),
        price_impact_pct=impact,
        min_amount_out=_float_from_units(min_ask, decimals_out),
        route=route,
        offer_units=str(data.get("offer_units") or units),
        min_ask_units=str(min_ask),
        router_address=router_addr,
        pool_address=pool,
        ask_units=str(ask_units),
        offer_jetton_wallet=str(data.get("offer_jetton_wallet") or ""),
        ask_jetton_wallet=str(data.get("ask_jetton_wallet") or ""),
        pton_wallet_address=str(router.get("pton_wallet_address") or ""),
        pton_master_address=str(router.get("pton_master_address") or ""),
        router_major_version=major,
        raw=data,
    )


def _build_stonfi_v1_swap_body(
    ask_jetton_wallet: str,
    min_out_units: int,
    user_address: str,
) -> Cell:
    """
    STON.fi v1 DexPayload for swap:
      swap#25938561 token_wallet1:MsgAddress min_out:Grams to_address:MsgAddress
                    ref_address:(Maybe MsgAddress)
    """
    b = Builder()
    b.store_uint(OP_STONFI_SWAP_V1, 32)
    b.store_address(_addr(ask_jetton_wallet))
    b.store_coins(int(min_out_units))
    b.store_address(_addr(user_address))
    b.store_bit(0)  # no referral
    return b.end_cell()


def _build_jetton_transfer(
    router_address: str,
    jetton_amount_units: int,
    forward_ton: int,
    forward_payload: Cell,
    user_address: str,
    query_id: int,
) -> Cell:
    jw = JettonWallet()
    return jw.create_transfer_body(
        to_address=_addr(router_address),
        jetton_amount=int(jetton_amount_units),
        forward_amount=int(forward_ton),
        forward_payload=forward_payload.to_boc(False),
        response_address=_addr(user_address),
        query_id=int(query_id),
    )


def _build_pton_ton_transfer(
    ton_amount_units: int,
    refund_address: str,
    forward_payload: Cell,
    query_id: int,
) -> Cell:
    """
    pTON ton_transfer#01f3835d query_id:uint64 ton_amount:Coins
      refund_address:MsgAddress forward_payload:(Either Cell ^Cell)
    """
    b = Builder()
    b.store_uint(OP_PTON_TON_TRANSFER, 32)
    b.store_uint(int(query_id), 64)
    b.store_coins(int(ton_amount_units))
    b.store_address(_addr(refund_address))
    b.store_bit(1)  # forward_payload as ref
    b.store_ref(forward_payload)
    return b.end_cell()


async def _get_seqno(address: str) -> int:
    """Fetch wallet seqno from tonapi (or 0 if wallet never initialized)."""
    base = "https://tonapi.io/v2" if config.TON_NETWORK == "mainnet" else "https://testnet.tonapi.io/v2"
    headers = {"Authorization": f"Bearer {config.TON_API_KEY}"} if config.TON_API_KEY else {}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{base}/wallet/{address}/seqno", headers=headers)
        if r.status_code in (400, 404):
            return 0
        r.raise_for_status()
        data = r.json()
        return int(data.get("seqno") or 0)


async def _get_jetton_wallet_address(owner: str, jetton_master: str) -> str:
    """Resolve the owner's jetton-wallet for a given master via tonapi."""
    base = "https://tonapi.io/v2" if config.TON_NETWORK == "mainnet" else "https://testnet.tonapi.io/v2"
    headers = {"Authorization": f"Bearer {config.TON_API_KEY}"} if config.TON_API_KEY else {}
    friendly_master = addr_utils.to_friendly(jetton_master) or jetton_master
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{base}/accounts/{owner}/jettons/{friendly_master}",
            headers=headers,
        )
        if r.status_code in (400, 404):
            raise RuntimeError("Could not find your jetton wallet for this token. Deposit some first.")
        r.raise_for_status()
        data = r.json()
        # tonapi returns wallet_address in several shapes depending on version
        w = (data.get("wallet_address") or {})
        if isinstance(w, dict):
            addr = w.get("address") or w.get("bounceable") or ""
        else:
            addr = str(w or "")
        if not addr:
            # fallback: balances list style
            addr = ((data.get("jetton") or {}).get("address")) or ""
        friendly = addr_utils.to_friendly(addr)
        if not friendly:
            raise RuntimeError("Could not resolve jetton wallet address.")
        return friendly


async def _broadcast_boc(boc_b64: str) -> str:
    """Send a signed external message BOC. Prefer toncenter, fall back to tonapi."""
    headers_json = {"Content-Type": "application/json"}
    if config.TON_API_KEY:
        headers_json["X-API-Key"] = config.TON_API_KEY

    # toncenter
    tc_base = (
        "https://toncenter.com/api/v2"
        if config.TON_NETWORK == "mainnet"
        else "https://testnet.toncenter.com/api/v2"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(
                f"{tc_base}/sendBoc",
                json={"boc": boc_b64},
                headers=headers_json,
                params={"api_key": config.TON_API_KEY} if config.TON_API_KEY else None,
            )
            data = r.json()
            if data.get("ok"):
                # toncenter may return hash in result
                result = data.get("result") or {}
                if isinstance(result, dict) and result.get("hash"):
                    return result["hash"]
                if isinstance(result, str) and result:
                    return result
                return "submitted"
            err = data.get("error") or data.get("result") or r.text[:200]
            logger.warning("toncenter sendBoc: %s", err)
        except Exception as e:
            logger.warning("toncenter broadcast failed: %s", e)

        # tonapi fallback
        ta_base = "https://tonapi.io/v2" if config.TON_NETWORK == "mainnet" else "https://testnet.tonapi.io/v2"
        ta_headers = {"Content-Type": "application/json"}
        if config.TON_API_KEY:
            ta_headers["Authorization"] = f"Bearer {config.TON_API_KEY}"
        r = await client.post(
            f"{ta_base}/blockchain/message",
            json={"boc": boc_b64},
            headers=ta_headers,
        )
        if r.status_code >= 400:
            detail = r.text[:300]
            raise RuntimeError(f"Broadcast failed: {detail}")
        data = r.json() if r.content else {}
        return (data.get("hash") if isinstance(data, dict) else None) or "submitted"


async def execute_swap(
    wallet_mnemonic: str,
    quote: Quote,
    user_wallet_address: str,
    decimals_in: int = 9,
) -> str:
    """
    Build, sign and broadcast a STON.fi swap for the given quote.
    Returns a tx hash / submission id string.
    """
    if not quote.router_address:
        raise RuntimeError("Quote is missing router address — request a fresh quote.")

    words = wallet_mnemonic.strip().split()
    _, _, _, wallet = Wallets.from_mnemonics(words, WalletVersionEnum.v4r2, 0)
    derived = wallet.address.to_string(is_user_friendly=True, is_bounceable=False)
    if not addr_utils.same(derived, user_wallet_address):
        raise RuntimeError("Wallet key does not match the active wallet address.")

    seqno = await _get_seqno(user_wallet_address)
    query_id = int(time.time()) % (2**32)

    offer_units = int(quote.offer_units)
    min_ask_units = int(quote.min_ask_units)
    is_ton_in = quote.token_in == "TON"
    is_ton_out = quote.token_out == "TON"

    # Ask-side jetton wallet on the router (for the token we receive, or pTON if out=TON)
    ask_jw = quote.ask_jetton_wallet
    if not ask_jw:
        raise RuntimeError("Quote missing ask_jetton_wallet — cannot build swap.")

    swap_body = _build_stonfi_v1_swap_body(
        ask_jetton_wallet=ask_jw,
        min_out_units=min_ask_units,
        user_address=user_wallet_address,
    )

    if is_ton_in:
        # TON → Jetton via pTON
        pton_wallet = quote.pton_wallet_address or quote.offer_jetton_wallet
        if not pton_wallet:
            raise RuntimeError("Quote missing pTON wallet — cannot swap TON.")
        # value = offer + forward gas; body = pTON ton_transfer wrapping swap
        body = _build_pton_ton_transfer(
            ton_amount_units=offer_units,
            refund_address=user_wallet_address,
            forward_payload=swap_body,
            query_id=query_id,
        )
        value = offer_units + int(GAS_TON_TO_JETTON_FWD)
        to_addr = pton_wallet
    else:
        # Jetton → (Jetton|TON): jetton transfer to router with swap forward payload
        user_jetton_wallet = await _get_jetton_wallet_address(user_wallet_address, quote.token_in)
        fwd = int(GAS_JETTON_TO_TON_FWD if is_ton_out else GAS_JETTON_TO_JETTON_FWD)
        gas = int(GAS_JETTON_TO_TON if is_ton_out else GAS_JETTON_TO_JETTON)
        body = _build_jetton_transfer(
            router_address=quote.router_address,
            jetton_amount_units=offer_units,
            forward_ton=fwd,
            forward_payload=swap_body,
            user_address=user_wallet_address,
            query_id=query_id,
        )
        value = gas
        to_addr = user_jetton_wallet

    query = wallet.create_transfer_message(
        to_addr=to_addr,
        amount=value,
        seqno=seqno,
        payload=body,
    )
    boc_b64 = bytes_to_b64str(query["message"].to_boc(False))
    tx_hash = await _broadcast_boc(boc_b64)
    logger.info(
        "swap broadcast ok route=%s in=%s out=%s hash=%s",
        quote.route, quote.token_in, quote.token_out, tx_hash,
    )
    return str(tx_hash)


async def send_ton(
    wallet_mnemonic: str,
    to_address: str,
    amount_ton: float,
    from_address: str,
    comment: str | None = None,
) -> str:
    """Simple TON transfer (used to collect the bot fee)."""
    if amount_ton <= 0:
        return ""
    words = wallet_mnemonic.strip().split()
    _, _, _, wallet = Wallets.from_mnemonics(words, WalletVersionEnum.v4r2, 0)
    seqno = await _get_seqno(from_address)
    payload = comment or ""
    query = wallet.create_transfer_message(
        to_addr=to_address,
        amount=to_nano(amount_ton, "ton"),
        seqno=seqno,
        payload=payload,
    )
    boc_b64 = bytes_to_b64str(query["message"].to_boc(False))
    return await _broadcast_boc(boc_b64)
