"""
DEX integration — STON.fi / DeDust quote + swap execution.

STATUS: STUBBED. Wire this to the real router SDK/API before any live
trading. Test on testnet first. Do not point this at mainnet funds until
get_quote() and execute_swap() are backed by real, tested integrations —
a bad stub here means users' TON goes into a transaction that doesn't do
what the UI told them it would.

STON.fi docs: https://docs.ston.fi
DeDust docs:  https://docs.dedust.io
"""

from dataclasses import dataclass
from config import config


@dataclass
class Quote:
    token_in: str
    token_out: str
    amount_in: float
    amount_out_estimated: float
    price_impact_pct: float
    min_amount_out: float  # after slippage
    route: str  # e.g. "STON.fi direct"


async def get_quote(token_in: str, token_out: str, amount_in: float, slippage_bps: int) -> Quote:
    """
    TODO: call STON.fi/DeDust quote endpoint.
    Example (STON.fi REST): GET /v1/swap/simulate?offer_address=...&ask_address=...&units=...
    """
    raise NotImplementedError(
        "Wire get_quote() to STON.fi or DeDust before enabling trading. "
        f"Configured provider: {config.DEX_PROVIDER}"
    )


async def execute_swap(
    wallet_mnemonic: str,
    quote: Quote,
) -> str:
    """
    Builds and sends the swap transaction using the user's wallet, signed
    server-side with the decrypted mnemonic (custodial model — see
    IMPORTANT.md). Returns tx hash.

    TODO: build the actual jetton transfer / swap message per the chosen
    router's contract interface, sign with tonsdk, broadcast via
    toncenter/tonapi.
    """
    raise NotImplementedError("Wire execute_swap() to the chosen DEX router before going live.")
