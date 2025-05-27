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
async def check_pumpfun_liquidity_and_marketcap(mint_address: str) -> int:
    """
    Calls Pump.fun's /coins/latest endpoint. Handles both single-object and list responses.
    Then:
      • Requires real_sol_reserves ≥ 1 SOL
      • If market_cap ≥ $50 → return 60 (hold 60s)
      • Elif market_cap ≥ $30 → return 20 (hold 20s)
      • Else → return 0 (skip)

    On any error or unexpected format → return 0.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(PUMP_FUN_API, timeout=7) as resp:
                data = await resp.json()

        # Handle case where a single token object is returned
        if isinstance(data, dict) and data.get("mint") == mint_address:
            token = data
            
        # Handle case where a token list is returned
        elif isinstance(data, list):
            token = next((t for t in data if t.get("mint") == mint_address), None)
            if token is None:
                logger.warning(f"[❌PUMP.FUN] Token {mint_address} not found in Pump.fun latest list.")
                return 0
        else:
            logger.error(f"[🚫PUMP.FUN FORMAT] Unexpected response format from Pump.fun API.")
            return 0

        # Extract reserves + market cap
        reserve_lamports = float(token.get("real_sol_reserves", 0))
        reserve_sol = reserve_lamports / 1e9
        market_cap = float(token.get("market_cap", 0))

        # 1) Liquidity check: require ≥ 1 SOL in reserves
        if reserve_sol < 1:
            logger.warning(f"[❌LIQUIDITY CHECK] {mint_address} has only {reserve_sol:.4f} SOL (< 1 SOL).")
            return 0
        else:
            logger.info(f"[✅LIQUIDITY CHECK] {mint_address} has {reserve_sol:.4f} SOL in reserve.")

        # 2) Market cap tiered hold time:
        if market_cap >= 50:
            logger.info(f"[✅MARKET CAP CHECK] {mint_address} has market cap ${market_cap:.2f} (≥ $50) → hold 60s.")
            return 60
        elif market_cap >= 30:
            logger.info(f"[✅MARKET CAP CHECK] {mint_address} has market cap ${market_cap:.2f} (≥ $30) → hold 20s.")
            return 20
        else:
            logger.warning(f"[❌MARKET CAP CHECK] {mint_address} has market cap ${market_cap:.2f} (< $30).")
            return 0

    except Exception as e:
        logger.error(f"[🚫PUMP.FUN CHECK] Error checking liquidity/market cap for {mint_address}: {e}")
        return 0

# ── Final Check Sequence ───────────────────────────────────────────────────────
async def passes_all_checks(mint_address: str) -> int:
    """
    Run safety checks in order:
      1. check_freeze_authority(mint_address)
      2. check_pumpfun_liquidity_and_marketcap(mint_address)

    If both pass, return the hold duration (20 or 60). Otherwise return 0.
    """
    try:
        if not await check_freeze_authority(mint_address):
            logger.info(f"[❌ALL CHECKS] {mint_address} failed freeze authority check.")
            return 0

        hold_time = await check_pumpfun_liquidity_and_marketcap(mint_address)
        if hold_time == 0:
            logger.info(f"[❌ALL CHECKS] {mint_address} failed liquidity/market cap check.")
            return 0

        logger.info(f"[✅ALL CHECKS] {mint_address} passed all checks (hold {hold_time}s).")
        return hold_time

    except Exception as e:
        logger.error(f"[🚫ALL CHECKS] Unexpected error for {mint_address}: {e}")
        return 0