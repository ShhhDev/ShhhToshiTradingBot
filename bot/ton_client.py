"""
TON chain interactions: wallet generation/import, balance + token holdings
reads, token metadata/market data, and USD prices. Swap-specific logic
lives in dex.py.

Reads go through tonapi.io (set TON_API_KEY for a higher rate limit) and
DexScreener (free, no key). Every network call goes through _tonapi_get(),
which retries on rate limits / server errors and raises TonApiError with a
readable message instead of failing silently.
"""

import asyncio
import logging
import time

import httpx
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
from tonsdk.crypto import mnemonic_new

import addr_utils
from config import config

logger = logging.getLogger(__name__)

TONAPI_BASE = "https://tonapi.io/v2" if config.TON_NETWORK == "mainnet" else "https://testnet.tonapi.io/v2"

_META_TTL_SECONDS = 600
_meta_cache: dict[str, tuple[float, dict]] = {}


class TonApiError(Exception):
    """Raised when the chain data provider can't be reached or rejects us."""


# ----------------------------------------------------------------------------
# Wallet creation / import
# ----------------------------------------------------------------------------

def create_new_wallet() -> tuple[str, str]:
    """Generates a new mnemonic + wallet address. Returns (mnemonic_str, address)."""
    mnemonic = mnemonic_new()
    _, _, _, wallet = Wallets.from_mnemonics(mnemonic, WalletVersionEnum.v4r2, 0)
    address = wallet.address.to_string(is_user_friendly=True, is_bounceable=False)
    return " ".join(mnemonic), address


def import_wallet_from_mnemonic(mnemonic_str: str) -> str:
    """Validates a mnemonic and returns the derived address. Raises ValueError if invalid."""
    words = mnemonic_str.strip().split()
    if len(words) not in (12, 24):
        raise ValueError("Seed phrase must be 12 or 24 words.")
    try:
        _, _, _, wallet = Wallets.from_mnemonics(words, WalletVersionEnum.v4r2, 0)
    except Exception as e:
        raise ValueError(f"Invalid seed phrase: {e}")
    return wallet.address.to_string(is_user_friendly=True, is_bounceable=False)


# ----------------------------------------------------------------------------
# HTTP helper
# ----------------------------------------------------------------------------

async def _tonapi_get(path: str, params: dict | None = None) -> dict | None:
    """
    GET a tonapi.io path. Returns parsed JSON, or None if the entity doesn't
    exist (404/400 - e.g. a brand-new wallet that has never been active, or a
    bad token address). Retries on 429 / 5xx / network errors, then raises
    TonApiError.
    """
    headers = {"Authorization": f"Bearer {config.TON_API_KEY}"} if config.TON_API_KEY else {}
    last_error = "unknown error"

    async with httpx.AsyncClient(timeout=15) as client:
        for attempt in range(3):
            try:
                r = await client.get(f"{TONAPI_BASE}{path}", headers=headers, params=params)
            except httpx.HTTPError as e:
                last_error = f"network error: {type(e).__name__}"
                await asyncio.sleep(1 + attempt)
                continue

            if r.status_code in (400, 404):
                return None
            if r.status_code in (401, 403):
                raise TonApiError("TonAPI rejected the request - check the TON_API_KEY variable.")
            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"TonAPI busy (HTTP {r.status_code})"
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            try:
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPStatusError, ValueError) as e:
                raise TonApiError(f"TonAPI error: {e}") from e

    raise TonApiError(last_error)


# ----------------------------------------------------------------------------
# Balances
# ----------------------------------------------------------------------------

async def get_ton_balance(address: str) -> float:
    """TON balance. A wallet that has never received anything returns 0.0."""
    data = await _tonapi_get(f"/accounts/{address}")
    if not data:
        return 0.0
    return int(data.get("balance", 0)) / 1e9


async def get_jetton_holdings(address: str) -> list[dict]:
    """
    Every jetton the wallet holds with a non-zero balance:
    [{symbol, name, contract, raw, balance, decimals, verification}]
    `contract` is the 48-char friendly address (safe for buttons); `raw` is
    the canonical raw form used for stable comparisons / DB keys.
    """
    data = await _tonapi_get(f"/accounts/{address}/jettons")
    if not data:
        return []

    holdings = []
    for item in data.get("balances", []):
        jetton = item.get("jetton") or {}
        raw_addr = jetton.get("address", "")
        friendly = addr_utils.to_friendly(raw_addr)
        if not friendly:
            continue
        decimals = int(jetton.get("decimals", 9))
        balance = int(item.get("balance", 0)) / (10 ** decimals)
        if balance <= 0:
            continue
        holdings.append({
            "symbol": jetton.get("symbol") or "???",
            "name": jetton.get("name") or "Unknown",
            "contract": friendly,
            "raw": addr_utils.to_raw(raw_addr),
            "balance": balance,
            "decimals": decimals,
            "verification": jetton.get("verification", "none"),
        })
    return holdings


async def get_usd_prices(jetton_addrs: list[str]) -> dict:
    """
    Best-effort USD prices from tonapi /rates. Returns {"TON": price, "<raw addr>": price, ...}.
    Anything tonapi can't price is simply missing from the result; never raises.
    """
    tokens = ["ton"]
    for a in jetton_addrs[:30]:
        f = addr_utils.to_friendly(a)
        if f:
            tokens.append(f)
    try:
        data = await _tonapi_get("/rates", params={"tokens": ",".join(tokens), "currencies": "usd"})
    except TonApiError as e:
        logger.warning(f"price lookup failed: {e}")
        return {}

    out: dict = {}
    for key, val in ((data or {}).get("rates") or {}).items():
        price = ((val or {}).get("prices") or {}).get("USD")
        if price is None:
            continue
        if str(key).upper() == "TON":
            out["TON"] = float(price)
        else:
            raw = addr_utils.to_raw(key)
            if raw:
                out[raw] = float(price)
    return out


# ----------------------------------------------------------------------------
# Token info
# ----------------------------------------------------------------------------

async def get_token_metadata(contract_address: str) -> dict | None:
    """Look up a jetton by address (raw or friendly). None if it isn't a jetton."""
    friendly = addr_utils.to_friendly(contract_address)
    if not friendly:
        return None

    cached = _meta_cache.get(friendly)
    if cached and time.time() - cached[0] < _META_TTL_SECONDS:
        return cached[1]

    data = await _tonapi_get(f"/jettons/{friendly}")
    if not data:
        return None

    meta = data.get("metadata") or {}
    decimals = int(meta.get("decimals", 9))
    result = {
        "symbol": meta.get("symbol") or "???",
        "name": meta.get("name") or "Unknown",
        "contract": friendly,
        "decimals": decimals,
        "total_supply": int(data.get("total_supply", 0)) / (10 ** decimals),
        "holders_count": data.get("holders_count"),
        "verification": data.get("verification", "none"),
    }
    _meta_cache[friendly] = (time.time(), result)
    return result


async def get_token_market_data(contract_address: str) -> dict | None:
    """
    Price / market cap / liquidity / 24h volume + holder count for a jetton.

    Price data comes from DexScreener (free, aggregates STON.fi + DeDust pools);
    holder count and on-chain metadata from tonapi. If DexScreener has no pool
    for the token, the market fields are None (shown as "-", never a fake $0).
    """
    meta = await get_token_metadata(contract_address)
    if meta is None:
        return None

    market = {
        "price_usd": None,
        "market_cap_usd": None,
        "liquidity_usd": None,
        "volume_24h_usd": None,
        "price_change_24h_pct": None,
        "dex": None,
        "pair_url": None,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{meta['contract']}")
            if r.status_code == 200:
                pairs = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "ton"]
                # prefer pools where this token is the priced (base) side
                base_pairs = [
                    p for p in pairs
                    if addr_utils.same((p.get("baseToken") or {}).get("address", ""), meta["contract"])
                ]
                pairs = base_pairs or pairs
                if pairs:
                    best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
                    if best.get("priceUsd"):
                        market["price_usd"] = float(best["priceUsd"])
                    market["market_cap_usd"] = best.get("marketCap") or best.get("fdv")
                    market["liquidity_usd"] = (best.get("liquidity") or {}).get("usd")
                    market["volume_24h_usd"] = (best.get("volume") or {}).get("h24")
                    market["price_change_24h_pct"] = (best.get("priceChange") or {}).get("h24")
                    market["dex"] = best.get("dexId")
                    market["pair_url"] = best.get("url")
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        pass  # DexScreener down / token not indexed yet - leave market fields as None

    return {**meta, **market}
