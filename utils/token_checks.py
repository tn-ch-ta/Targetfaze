# utils/token_checks.py

import httpx
_original_async_init = httpx.AsyncClient.__init__
def _patched_async_init(self, *args, proxy=None, **kwargs):
    return _original_async_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_async_init

import logging
import random
import asyncio
import aiohttp
from solders.pubkey import Pubkey
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
PUMP_FUN_API = "https://frontend-api-v3.pump.fun/coins/latest"
JUPITER_TOKEN_INFO_API = "https://lite-api.jup.ag/tokens/v1/token/{}"

# ── RPC Client Selector ───────────────────────────────────────────────────────
def get_random_client() -> AsyncClient:
    rpc_url = random.choice(SOLANA_RPC_URLS)
    return AsyncClient(rpc_url)

# ── Freeze Authority Check (Jupiter) ──────────────────────────────────────────
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

# ── Liquidity + Market Cap Check (Pump.fun) ───────────────────────────────────
async def check_pumpfun_liquidity_and_marketcap(mint_address: str) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(PUMP_FUN_API, timeout=7) as resp:
                data = await resp.json()

        # Handle case where a single token is returned
        if isinstance(data, dict) and data.get("mint") == mint_address:
            token = data
        # Handle case where a list of tokens is returned (older behavior)
        elif isinstance(data, list):
            token = next((t for t in data if t.get("mint") == mint_address), None)
            if token is None:
                logger.warning(f"[❌PUMP.FUN] Token {mint_address} not found in Pump.fun latest list.")
                return False
        else:
            logger.error(f"[🚫PUMP.FUN FORMAT] Unexpected response format from Pump.fun")
            return False

        reserve_lamports = float(token.get("real_sol_reserves", 0))
        reserve_sol = reserve_lamports / 1e9
        market_cap = float(token.get("market_cap", 0))

        if reserve_sol < 1:
            logger.warning(f"[❌LIQUIDITY CHECK] {mint_address} has only {reserve_sol:.4f} SOL (< 1 SOL).")
            return False
        else:
            logger.info(f"[✅LIQUIDITY CHECK] {mint_address} has {reserve_sol:.4f} SOL.")

        if market_cap < 30:
            logger.warning(f"[❌MARKET CAP CHECK] {mint_address} has market cap ${market_cap:.2f} (< $30).")
            return False
        else:
            logger.info(f"[✅MARKET CAP CHECK] {mint_address} has market cap ${market_cap:.2f}.")

        return True

    except Exception as e:
        logger.error(f"[🚫PUMP.FUN CHECK] Error checking liquidity/market cap for {mint_address}: {e}")
        return False

# ── Final Check Sequence ───────────────────────────────────────────────────────
async def passes_all_checks(mint_address: str) -> int:
    """
    Run safety checks:
     1. Freeze authority check (Jupiter)
     2. real_sol_reserves ≥ 1 SOL (Pump.fun)
     3. market_cap ≥ $30 (Pump.fun)

    If all pass → return 60s hold time
    Else → return 0
    """
    try:
        if not await check_freeze_authority(mint_address):
            logger.info(f"[❌ALL CHECKS] {mint_address} failed freeze authority check.")
            return 0

        if not await check_pumpfun_liquidity_and_marketcap(mint_address):
            logger.info(f"[❌ALL CHECKS] {mint_address} failed liquidity/market cap check.")
            return 0

        logger.info(f"[✅ALL CHECKS] {mint_address} passed all checks (hold 60s).")
        return 60

    except Exception as e:
        logger.error(f"[🚫ALL CHECKS] Unexpected error for {mint_address}: {e}")
        return 0