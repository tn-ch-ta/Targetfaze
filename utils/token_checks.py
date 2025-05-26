# utils/token_checks.py

import httpx
_original_async_init = httpx.AsyncClient.__init__
def _patched_async_init(self, *args, proxy=None, **kwargs):
    return _original_async_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_async_init

import logging
import asyncio
import random
import aiohttp
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("token_checks")

# Constants
SOLANA_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://api.metaplex.solana.com"
]
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://lite-api.jup.ag/swap/v1/quote"

def get_random_client() -> AsyncClient:
    rpc_url = random.choice(SOLANA_RPC_URLS)
    return AsyncClient(rpc_url)


async def is_token_rug(mint_address: str) -> bool:
    params = {
        "inputMint": mint_address,
        "outputMint": SOL_MINT,
        "amount": 1000,
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


async def check_liquidity(mint_address: str, min_sol: float = 0.5) -> bool:
    lam = int(min_sol * 1e9)
    params = {
        "inputMint": SOL_MINT,
        "outputMint": mint_address,
        "amount": lam,
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
            logger.info(f"[❌LIQUIDITY CHECK] {mint_address} has 0 outAmount (no liquidity).")
            return False

        logger.info(f"[✅LIQUIDITY CHECK] {mint_address} has liquidity (outAmount={out_amount}).")
        return True
    except Exception as e:
        logger.error(f"[🚫LIQUIDITY CHECK] Error for {mint_address}: {e}")
        return False


async def passes_all_checks(mint_address: str) -> bool:
    try:
        is_rug_flag = await is_token_rug(mint_address)
        liquidity_ok = await check_liquidity(mint_address)

        if is_rug_flag:
            logger.info(f"[❌ALL CHECKS] {mint_address} failed: Rug detected.")
            return False
        if not liquidity_ok:
            logger.info(f"[❌ALL CHECKS] {mint_address} failed: Insufficient liquidity.")
            return False

        logger.info(f"[✅ALL CHECKS] {mint_address} passed all checks.")
        return True
    except Exception as e:
        logger.error(f"[🚫ALL CHECKS] Error for {mint_address}: {e}")
        return False