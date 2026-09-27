import os
from dotenv import load_dotenv

load_dotenv()  # looks for .env in the current working directory (bot/) or any parent folder


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    ADMIN_TELEGRAM_IDS = {
        int(x) for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip().isdigit()
    }

    MASTER_ENCRYPTION_KEY = os.getenv("MASTER_ENCRYPTION_KEY", "")
    ADMIN_PANEL_SECRET = os.getenv("ADMIN_PANEL_SECRET", "change-me")

    TON_NETWORK = os.getenv("TON_NETWORK", "mainnet")
    TON_API_KEY = os.getenv("TON_API_KEY", "")

    DEV_FEE_WALLET = os.getenv(
        "DEV_FEE_WALLET", "UQALrB2fpRmfx3HWpmzPz3zfNsAiJNpP0-4J6ENptLel_e7w"
    )

    DEX_PROVIDER = os.getenv("DEX_PROVIDER", "stonfi")
    STONFI_API_URL = os.getenv("STONFI_API_URL", "https://api.ston.fi")
    DEDUST_API_URL = os.getenv("DEDUST_API_URL", "https://api.dedust.io")

    # Fee is stored in DB (FeeConfig table) so admin panel can change it live.
    # This is just the seed value used on first boot / table init.
    DEFAULT_FEE_BPS = _int("DEFAULT_FEE_BPS", 500)  # 500 bps = 5%

    MAX_TRADE_TON = _int("MAX_TRADE_TON", 0)  # 0 = no cap
    MAX_DAILY_VOLUME_TON = _int("MAX_DAILY_VOLUME_TON", 0)
    LARGE_TRADE_CONFIRM_THRESHOLD_TON = _int("LARGE_TRADE_CONFIRM_THRESHOLD_TON", 1000)

    # On Railway, add a Postgres plugin and it injects DATABASE_URL automatically
    # (as postgresql://...). We rewrite it to the async driver SQLAlchemy needs.
    _raw_db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./shhhtoshi.db")
    if _raw_db_url.startswith("postgres://"):
        _raw_db_url = _raw_db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif _raw_db_url.startswith("postgresql://") and "+asyncpg" not in _raw_db_url:
        _raw_db_url = _raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    DATABASE_URL = _raw_db_url


config = Config()
