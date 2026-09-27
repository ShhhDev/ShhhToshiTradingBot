"""
Fee calculation. Fee % is read from the DB (FeeConfig table), which the
admin panel edits live — NOT from .env — so changes apply instantly
without a redeploy.
"""

from sqlalchemy import select
from db import async_session, FeeConfig


async def get_fee_config() -> FeeConfig:
    async with async_session() as session:
        result = await session.execute(select(FeeConfig).where(FeeConfig.id == 1))
        fee_config = result.scalar_one()
        return fee_config


def calculate_fee(amount_ton: float, fee_bps: int) -> tuple[float, float]:
    """Returns (fee_amount, amount_after_fee)."""
    fee_amount = amount_ton * (fee_bps / 10_000)
    return fee_amount, amount_ton - fee_amount


async def check_trade_allowed(amount_ton: float, user_daily_volume_ton: float) -> tuple[bool, str | None]:
    """Checks global kill switch + configured caps (0 = no cap, per current spec)."""
    fc = await get_fee_config()

    if not fc.trading_enabled:
        return False, "Trading is currently paused. Please try again later."

    if fc.max_trade_ton and amount_ton > float(fc.max_trade_ton):
        return False, f"This trade exceeds the current max trade size ({fc.max_trade_ton} TON)."

    if fc.max_daily_volume_ton and (user_daily_volume_ton + amount_ton) > float(fc.max_daily_volume_ton):
        return False, f"This would exceed your daily trading limit ({fc.max_daily_volume_ton} TON)."

    return True, None


async def needs_large_trade_confirmation(amount_ton: float) -> bool:
    fc = await get_fee_config()
    threshold = float(fc.large_trade_confirm_threshold_ton)
    return threshold > 0 and amount_ton >= threshold
