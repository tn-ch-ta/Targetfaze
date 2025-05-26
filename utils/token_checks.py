# utils/token_checks.py

import httpx
_original_async_init = httpx.AsyncClient.__init__
def _patched_async_init(self, *args, proxy=None, **kwargs):
    return _original_async_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_async_init

import logging
import random
import aiohttp
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("token_checks")

# ── Constants ─────────────────────────────────────────────────────────────────
SOLANA_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://api.metaplex.solana.com"
]
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_TOKEN_INFO_API = "https://lite-api.jup.ag/tokens/v1/token/{}"

BURN_ADDRESSES = {
    "11111111111111111111111111111111",
    "SysvarRent111111111111111111111111111111111",
    "SysvarC1ock11111111111111111111111111111111",
    "1nc1nerator11111111111111111111111111111111"
}


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


async def check_token_holder_distribution(mint_address: str) -> int:
    """
    Analyze token distribution by inspecting the top holders of the token mint.
    Heuristic:
        - Top holder is burn address → safe → hold 60s
        - Top holder > 70% → centralized → hold 25s
        - Top 3 holders > 90% → risky → hold 25s
        - Otherwise → decentralized → hold 60s
    Return 0 on error or empty results.
    """
    try:
        client = get_random_client()
        resp = await client.get_token_largest_accounts(Pubkey.from_string(mint_address))
        await client.close()

        holders = resp.value
        if not holders:
            logger.warning(f"[❌DISTRIBUTION CHECK] No holders found for {mint_address}.")
            return 0

        top_amounts = [h.ui_amount for h in holders if h.ui_amount]
        top_addresses = [h.address.to_string() for h in holders]
        total = sum(top_amounts)

        if total == 0:
            logger.warning(f"[❌DISTRIBUTION CHECK] Total token supply is 0 for {mint_address}.")
            return 0

        top_pct = top_amounts[0] / total
        top3_pct = sum(top_amounts[:3]) / total

        logger.info(f"[📊DISTRIBUTION CHECK] {mint_address} Top1={top_pct:.2%}, Top3={top3_pct:.2%}")
        

        if top_pct > 0.4:
            logger.warning(f"[⚠️DISTRIBUTION CHECK] Top holder owns >40% → Hold 25s")
            return 25

        if top3_pct > 0.9:
            logger.warning(f"[⚠️DISTRIBUTION CHECK] Top 3 holders own >90% → Hold 10s")
            return 10

        logger.info(f"[✅DISTRIBUTION CHECK] Holder distribution is decentralized → Hold 60s")
        return 60

    except Exception as e:
        logger.error(f"[🚫DISTRIBUTION CHECK] Error checking distribution for {mint_address}: {e}")
        return 0


async def passes_all_checks(mint_address: str) -> int:
    """
    Run all safety checks in sequence:
     1. is_token_rug()
     2. check_freeze_authority()
     3. check_token_holder_distribution()

    Returns:
      0  → fail/skip
      25 → buy + hold 25s
      50 → buy + hold 50s
      60 → buy + hold 60s
    """
    try:
        if await is_token_rug(mint_address):
            logger.info(f"[❌ALL CHECKS] {mint_address} is a rug.")
            return 0

        if not await check_freeze_authority(mint_address):
            logger.info(f"[❌ALL CHECKS] {mint_address} has freeze authority.")
            return 0

        hold_time = await check_token_holder_distribution(mint_address)
        if hold_time == 0:
            logger.info(f"[❌ALL CHECKS] {mint_address} failed distribution check.")
            return 0

        logger.info(f"[✅ALL CHECKS] {mint_address} passed all checks (hold {hold_time}s).")
        return hold_time

    except Exception as e:
        logger.error(f"[🚫ALL CHECKS] Unexpected error for {mint_address}: {e}")
        return 0