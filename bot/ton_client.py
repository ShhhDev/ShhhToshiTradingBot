"""
TON chain interactions: wallet generation/import, balance + token holdings
reads, and raw transfer building. Swap-specific logic lives in dex.py.

Uses tonsdk for key/wallet derivation. Swap network calls go through
toncenter.com / tonapi.io — set TON_API_KEY in .env.
"""

import httpx
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
from tonsdk.crypto import mnemonic_new, mnemonic_to_wallet_key

from config import config

TONAPI_BASE = "https://tonapi.io/v2" if config.TON_NETWORK == "mainnet" else "https://testnet.tonapi.io/v2"


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
        raise ValueError("Mnemonic must be 12 or 24 words.")
    try:
        _, _, _, wallet = Wallets.from_mnemonics(words, WalletVersionEnum.v4r2, 0)
    except Exception as e:
        raise ValueError(f"Invalid seed phrase: {e}")
    return wallet.address.to_string(is_user_friendly=True, is_bounceable=False)


async def get_ton_balance(address: str) -> float:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{TONAPI_BASE}/accounts/{address}",
            headers={"Authorization": f"Bearer {config.TON_API_KEY}"} if config.TON_API_KEY else {},
        )
        r.raise_for_status()
        data = r.json()
        return int(data.get("balance", 0)) / 1e9


async def get_jetton_holdings(address: str) -> list[dict]:
    """Returns list of {symbol, name, contract, balance, decimals} for all jettons held."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{TONAPI_BASE}/accounts/{address}/jettons",
            headers={"Authorization": f"Bearer {config.TON_API_KEY}"} if config.TON_API_KEY else {},
        )
        r.raise_for_status()
        data = r.json()

    holdings = []
    for item in data.get("balances", []):
        jetton = item.get("jetton", {})
        decimals = jetton.get("decimals", 9)
        raw_balance = int(item.get("balance", 0))
        holdings.append({
            "symbol": jetton.get("symbol", "???"),
            "name": jetton.get("name", "Unknown"),
            "contract": jetton.get("address", ""),
            "balance": raw_balance / (10 ** decimals),
            "decimals": decimals,
        })
    return holdings


async def get_token_metadata(contract_address: str) -> dict | None:
    """Look up a jetton by CA — used when a user pastes a contract address to buy."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{TONAPI_BASE}/jettons/{contract_address}",
            headers={"Authorization": f"Bearer {config.TON_API_KEY}"} if config.TON_API_KEY else {},
        )
        if r.status_code != 200:
            return None
        data = r.json()
        meta = data.get("metadata", {})
        total_supply_raw = int(data.get("total_supply", 0))
        decimals = int(meta.get("decimals", 9))
        return {
            "symbol": meta.get("symbol", "???"),
            "name": meta.get("name", "Unknown"),
            "contract": contract_address,
            "decimals": decimals,
            "total_supply": total_supply_raw / (10 ** decimals),
            "holders_count": data.get("holders_count"),  # tonapi returns this on the jetton endpoint
            "verified": data.get("verification") == "whitelist",
        }


async def get_token_market_data(contract_address: str) -> dict | None:
    """
    Price / market cap / liquidity / 24h volume + holder count for a jetton.

    Uses DexScreener's public API for price/liquidity/volume (free, no key,
    aggregates STON.fi + DeDust pools) and tonapi.io for holder count and
    on-chain metadata, then merges the two. If DexScreener has no pool for
    a token (too new / no liquidity yet), price/liquidity fields come back
    as None rather than fabricated numbers — the caller should render that
    as "No liquidity found" rather than showing a fake $0.
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
            r = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{contract_address}")
            if r.status_code == 200:
                data = r.json()
                pairs = data.get("pairs") or []
                # TON pairs only, pick the deepest-liquidity pool if several exist
                ton_pairs = [p for p in pairs if p.get("chainId") == "ton"]
                if ton_pairs:
                    best = max(
                        ton_pairs,
                        key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
                    )
                    market["price_usd"] = float(best["priceUsd"]) if best.get("priceUsd") else None
                    market["market_cap_usd"] = best.get("fdv") or best.get("marketCap")
                    liq = best.get("liquidity") or {}
                    market["liquidity_usd"] = liq.get("usd")
                    vol = best.get("volume") or {}
                    market["volume_24h_usd"] = vol.get("h24")
                    change = best.get("priceChange") or {}
                    market["price_change_24h_pct"] = change.get("h24")
                    market["dex"] = best.get("dexId")
                    market["pair_url"] = best.get("url")
    except (httpx.HTTPError, ValueError, KeyError):
        # DexScreener down or token not indexed there yet — leave market fields as None
        pass

    return {**meta, **market}
