# utils/token_checks.py

import httpx
_original_async_init = httpx.AsyncClient.__init__
def _patched_async_init(self, *args, proxy=None, **kwargs):
    return _original_async_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_async_init

import logging
import random
import aiohttp
from solana.rpc.async_api import AsyncClient

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("token_checks")

# ── Constants ─────────────────────────────────────────────────────────────────
SOLANA_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.g.alchemy.com/v2/NHAveomHS7q-QGj2ddOa86_QzVi9QzeY",
    "https://solana-mainnet.g.alchemy.com/v2/D6p4-dGHuCfO42nBFTPzJdpWBN9vUlsz",
    "https://solana-rpc.publicnode.com",
    "https://solana.drpc.org"
]

JUPITER_TOKEN_INFO_API = "https://lite-api.jup.ag/tokens/v1/token/{}"
BIRDEYE_API = "https://public-api.birdeye.so/defi/v3/token/market-data"
BIRDEYE_HEADERS = {
    "accept": "application/json",
    "x-chain": "solana",
    "X-API-KEY": "38a6df7fd63941b3aaf7e25f9e38a2e8"
}
RUGCHECK_API = "https://api.rugcheck.xyz/v1/tokens/{}/report"

# ── RPC Client Selector ───────────────────────────────────────────────────────
def get_random_client() -> AsyncClient:
    return AsyncClient(random.choice(SOLANA_RPC_URLS))

# ── Freeze Authority Check ────────────────────────────────────────────────────
async def check_freeze_authority(mint_address: str) -> bool:
    url = JUPITER_TOKEN_INFO_API.format(mint_address)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                data = await resp.json()

        freeze_auth = data.get("freeze_authority")
        if freeze_auth is None:
            logger.info(f"[✅FREEZE CHECK] {mint_address} has no freeze authority.")
            return True

        logger.warning(f"[❌FREEZE CHECK] {mint_address} has freeze authority: {freeze_auth}")
        return False

    except Exception as e:
        logger.error(f"[🚫FREEZE CHECK] Error checking freezeAuthority for {mint_address}: {e}")
        return False

# ── Liquidity + Market Cap Check ──────────────────────────────────────────────
async def check_birdeye_liquidity_and_marketcap(mint_address: str) -> tuple | None:
    params = {"address": mint_address, "ui_amount_mode": "scaled"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BIRDEYE_API, headers=BIRDEYE_HEADERS, params=params, timeout=7) as resp:
                data = await resp.json()

        market_cap = float(data.get("data", {}).get("market_cap", 0))
        liquidity = float(data.get("data", {}).get("liquidity", 0))

        if liquidity > market_cap:
            logger.warning(f"[❌BIRDEYE] {mint_address} liquidity {liquidity} > market cap {market_cap}.")
            return None
        if market_cap <= 2000:
            logger.warning(f"[❌BIRDEYE] {mint_address} market cap ${market_cap:.2f} <= 2000.")
            return None
        if liquidity <= 500:
            logger.warning(f"[❌BIRDEYE] {mint_address} liquidity ${liquidity:.2f} <= 500.")
            return None

        logger.info(f"[✅BIRDEYE] {mint_address} market cap ${market_cap:.2f}, liquidity ${liquidity:.2f}.")
        return liquidity, market_cap

    except Exception as e:
        logger.error(f"[🚫BIRDEYE] Error checking liquidity/market cap for {mint_address}: {e}")
        return None

# ── Rugcheck Score Check ──────────────────────────────────────────────────────
async def check_rugcheck_score(mint_address: str) -> bool:
    url = RUGCHECK_API.format(mint_address)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=7) as resp:
                data = await resp.json()

        score_norm = float(data.get("score_normalised", 999))
        if score_norm < 5:
            logger.info(f"[✅RUGCHECK] {mint_address} score_normalised {score_norm:.2f} < 2.")
            return True

        logger.warning(f"[❌RUGCHECK] {mint_address} score_normalised {score_norm:.2f} >= 2.")
        return False

    except Exception as e:
        logger.error(f"[🚫RUGCHECK] Error checking Rugcheck score for {mint_address}: {e}")
        return False

# ── Final Check Sequence ──────────────────────────────────────────────────────
async def passes_all_checks(mint_address: str) -> tuple | None:
    """
    Returns (liquidity, market_cap) if all checks pass, else None.
    """
    try:
        # 1. Freeze authority
        if not await check_freeze_authority(mint_address):
            return None

        # 2. Liquidity & market cap
        birdeye_result = await check_birdeye_liquidity_and_marketcap(mint_address)
        if birdeye_result is None:
            return None
        liquidity, market_cap = birdeye_result

        # 3. Rugcheck
        if not await check_rugcheck_score(mint_address):
            return None

        logger.info(f"[✅ALL CHECKS] {mint_address} passed all checks.")
        return liquidity, market_cap

    except Exception as e:
        logger.error(f"[🚫ALL CHECKS] Unexpected error for {mint_address}: {e}")
        return None