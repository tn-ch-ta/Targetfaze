# Targetfaze — Telegram-Powered Solana Pump.fun Sniper

Targetfaze is a fully automated memecoin sniper bot for the Solana network. It detects **new tokens** listed on [Pump.fun](https://pump.fun), runs 5 **real on-chain safety checks**, and **executes real buy/sell trades** via [Jupiter Aggregator](https://jup.ag) using Solana's `solders` for transaction signing.

---

## 🚀 Features

- 🟢 **Real-time token monitoring** from Pump.fun API
- ✅ **5 Real checks**: Honeypot, Freeze Authority, Liquidity, Insider Holders, Distribution Diversity
- 💸 **Base58 Phantom key support**
- ⚙️ **Real Jupiter swap logic** (no simulation)
- 🔄 **Auto-sell** after configurable profit %
- 📱 **Telegram interface** to start/stop/configure easily

---

## 📦 Installation

1. **Clone the repo**

```bash
git clone https://github.com/yourusername/Targetfaze.git
cd Targetfaze
