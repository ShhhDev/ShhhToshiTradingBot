from datetime import datetime
from sqlalchemy import (
    String, Integer, BigInteger, Numeric, DateTime, Boolean, ForeignKey, Text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from config import config


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    slippage_bps: Mapped[int] = mapped_column(Integer, default=100)  # 1% default
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)

    # Referrals
    referral_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    referred_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    referral_balance_ton: Mapped[float] = mapped_column(Numeric(38, 9), default=0)  # unclaimed, available now
    referral_earned_total_ton: Mapped[float] = mapped_column(Numeric(38, 9), default=0)  # lifetime, never decreases
    referral_claimed_total_ton: Mapped[float] = mapped_column(Numeric(38, 9), default=0)  # lifetime, paid out

    wallets: Mapped[list["Wallet"]] = relationship(back_populates="user", foreign_keys="Wallet.user_id")
    trades: Mapped[list["Trade"]] = relationship(back_populates="user")


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    address: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    # Envelope encryption: per-wallet random data key, wrapped by master key.
    encrypted_mnemonic: Mapped[str] = mapped_column(Text)
    wrapped_data_key: Mapped[str] = mapped_column(Text)

    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    imported: Mapped[bool] = mapped_column(Boolean, default=False)  # created vs imported

    user: Mapped["User"] = relationship(back_populates="wallets")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    wallet_address: Mapped[str] = mapped_column(String(128))

    trade_type: Mapped[str] = mapped_column(String(16))  # buy | sell | swap
    token_in: Mapped[str] = mapped_column(String(128))   # contract address or "TON"
    token_out: Mapped[str] = mapped_column(String(128))
    amount_in: Mapped[float] = mapped_column(Numeric(38, 9))
    amount_out: Mapped[float] = mapped_column(Numeric(38, 9))

    fee_bps_applied: Mapped[int] = mapped_column(Integer)
    fee_amount_ton: Mapped[float] = mapped_column(Numeric(38, 9))
    fee_tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|success|failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="trades")


class FeeConfig(Base):
    """Single-row table the admin panel edits live. Bot reads this, not .env, at runtime."""
    __tablename__ = "fee_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    fee_bps: Mapped[int] = mapped_column(Integer, default=500)  # 500 = 5%
    dev_wallet: Mapped[str] = mapped_column(String(128))
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # global kill switch
    max_trade_ton: Mapped[float] = mapped_column(Numeric(38, 9), default=0)  # 0 = no cap
    max_daily_volume_ton: Mapped[float] = mapped_column(Numeric(38, 9), default=0)
    large_trade_confirm_threshold_ton: Mapped[float] = mapped_column(Numeric(38, 9), default=1000)
    referral_share_bps: Mapped[int] = mapped_column(Integer, default=1000)  # 1000 = 10% of the fee -> referrer
    referral_min_claim_ton: Mapped[float] = mapped_column(Numeric(38, 9), default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_by_admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class DepositSnapshot(Base):
    """
    Last-seen balance per (wallet, asset) so the deposit watcher can diff
    fresh reads against this and detect newly-arrived funds. "TON" is used
    as the asset key for native TON; jettons are keyed by contract address.
    """
    __tablename__ = "deposit_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    asset: Mapped[str] = mapped_column(String(128))  # "TON" or jetton contract address
    symbol: Mapped[str] = mapped_column(String(32), default="TON")
    last_balance: Mapped[float] = mapped_column(Numeric(38, 9), default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ClaimRequest(Base):
    """A user's request to withdraw their referral balance. Admin approves or declines
    it by hand (per the spec: claims are not paid out automatically)."""
    __tablename__ = "claim_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount_ton: Mapped[float] = mapped_column(Numeric(38, 9))
    payout_address: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|declined
    admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


engine = create_async_engine(config.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # seed FeeConfig row if missing
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(FeeConfig).where(FeeConfig.id == 1))
        if result.scalar_one_or_none() is None:
            session.add(FeeConfig(
                id=1,
                fee_bps=config.DEFAULT_FEE_BPS,
                dev_wallet=config.DEV_FEE_WALLET,
                trading_enabled=True,
                max_trade_ton=config.MAX_TRADE_TON,
                max_daily_volume_ton=config.MAX_DAILY_VOLUME_TON,
                large_trade_confirm_threshold_ton=config.LARGE_TRADE_CONFIRM_THRESHOLD_TON,
                referral_share_bps=config.REFERRAL_SHARE_BPS,
                referral_min_claim_ton=config.REFERRAL_MIN_CLAIM_TON,
            ))
            await session.commit()

    # backfill referral_code for any users created before this column existed
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.referral_code.is_(None)))
        missing = result.scalars().all()
        if missing:
            existing = set((await session.execute(select(User.referral_code))).scalars().all())
            for user in missing:
                user.referral_code = _generate_unique_code(existing)
                existing.add(user.referral_code)
            await session.commit()


def _generate_unique_code(existing: set[str], length: int = 8) -> str:
    import secrets
    import string
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(50):
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        if code not in existing:
            return code
    raise RuntimeError("could not generate a unique referral code")
