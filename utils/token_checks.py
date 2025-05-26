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

# Configure logging
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
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_TOKEN_INFO_API = "https://lite-api.jup.ag/tokens/v1/token/{}"


def get_random_client() -> AsyncClient:
    rpc_url = random.choice(SOLANA_RPC_URLS)
    return AsyncClient(rpc_url)


async def is_token_rug(mint_address: str) -> bool:
    params = {
        "inputMint": mint_address,
        "outputMint": SOL_MINT,
        "amount": 100_000,
        "slippageBps": 100,
        "onlyDirectRoutes": "false",
        "restrictIntermediateTokens": "true",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(JUPITER_QUOTE_API, params=params, timeout=5) as resp:
                data = await resp.json()

        out_amount = int(data.get("outAmount", 0))
        if out_amount == 0:
            logger.info(f"[❌RUG CHECK] {mint_address} is a rug (outAmount=0).")
            return True

        logger.info(f"[✅RUG CHECK] {mint_address} passed rug check (outAmount={out_amount}).")
        return False

    except Exception as e:
        logger.error(f"[🚫RUG CHECK] Error checking rug status for {mint_address}: {e}")
        return True


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


async def passes_all_checks(mint_address: str) -> int:
    """
    Run safety checks:
     1. is_token_rug()
     2. check_freeze_authority()

    If passed → return 60s hold time
    Else → return 0 (skip)
    """
    try:
        if await is_token_rug(mint_address):
            logger.info(f"[❌ALL CHECKS] {mint_address} is a rug.")
            return 0

        if not await check_freeze_authority(mint_address):
            logger.info(f"[❌ALL CHECKS] {mint_address} has freeze authority.")
            return 0

        logger.info(f"[✅ALL CHECKS] {mint_address} passed all checks (hold 60s).")
        return 60

    except Exception as e:
        logger.error(f"[🚫ALL CHECKS] Unexpected error for {mint_address}: {e}")
        return 0