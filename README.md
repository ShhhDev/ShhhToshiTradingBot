# ShhhToshi — TON Trading Bot

<p align="center">
  <b>Created by ShhhDev</b>
</p>

A Telegram-native trading bot for the TON blockchain. Users create or
import a wallet directly in chat and trade tokens — buy, sell, swap —
without leaving Telegram, all through inline buttons.

---

## ✨ Features

- **In-chat wallet setup** — create a new TON wallet or import an
  existing one by seed phrase, no external app required
- **One-tap trading** — buy, sell, or swap any token by contract address
  or directly from your holdings
- **Live token details** — price, market cap, liquidity, 24h volume, and
  holder count shown before every trade
- **Full holdings view** — see your complete token balance and TON
  balance in one place
- **Built-in admin controls** — fee percentage, trade limits, and a
  trading kill switch, all managed from within the bot itself

## 🪙 About the ShhhToshi token

ShhhToshi is a community memecoin on the TON blockchain.

**Contract address:** `EQAesdqwmBcSUhfMEbNs5J3yxJCGEZD-82lu88Q2TL9kUpyW`

*(Always verify the contract address independently before trading. This
bot does not guarantee the safety or performance of any token, including
ShhhToshi.)*

## Stack
- Python 3.11+, aiogram 3.x
- SQLAlchemy + SQLite (local dev) / PostgreSQL (production)
- TON SDKs for wallet operations, STON.fi / DeDust for swaps
- AES envelope encryption for wallet key storage

## Structure
```
bot/
  main.py                  # entrypoint — run this file directly
  config.py                # environment configuration
  db.py                    # database models
  start_handlers.py        # /start, main menu, onboarding
  wallet_handlers.py       # create/import wallet flow
  balance_handlers.py      # holdings & balance view
  trade_handlers.py        # buy/sell/swap flows, token lookup by CA
  settings_handlers.py     # slippage, seed phrase export
  admin_handlers.py        # admin menu: fees, limits, kill switch, stats
  encryption.py            # wallet key encryption at rest
  ton_client.py            # on-chain reads, wallet ops, token market data
  dex.py                   # DEX swap integration
  fees.py                  # fee calculation, trade limit checks
IMPORTANT.md             operational & security notes
RAILWAY_DEPLOY.md        hosting guide
.env.example
```

## Running locally
```
pip install -r requirements.txt
cp .env.example .env   # fill in your own values — keep this at the repo root
cd bot
python main.py
```

## Admin access
Admin controls are built into the bot — message it `/admin` from an
authorized Telegram account to manage fees, trade limits, and view
stats. No separate dashboard or website required.

---

<p align="center">
  <sub>ShhhToshi — built by ShhhDev</sub>
</p>

