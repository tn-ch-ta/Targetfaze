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
    """
    Return an AsyncClient connected to a random RPC endpoint
    to help spread rate‐limits.
    """
    rpc_url = random.choice(SOLANA_RPC_URLS)
    return AsyncClient(rpc_url)


async def is_token_rug(mint_address: str) -> bool:
    """
    Check if a token is a honeypot/rug by asking Jupiter if you can swap a small
    amount (0.01 token units) back into SOL. If outAmount==0, treat as rug.
    """
    params = {
        "inputMint": mint_address,
        "outputMint": SOL_MINT,
        "amount": 100_000,      # 0.01 token units (assuming 9 decimals)
        "slippageBps": 100,       # 1%
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
        # On error, assume rug to be safe
        return True


async def check_freeze_authority(mint_address: str) -> bool:
    """
    Query Jupiter’s token info endpoint to see if a freezeAuthority exists.
    Returns True if freezeAuthority is null (safe), False if present (risky).
    """
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
        # On error, treat as “risky”
        return False


async def check_lp_ownership(mint_address: str) -> int:
    """
    Fetch the  largest token accounts of the given mint on‐chain.
    If top holder ∉ burn addresses        → hold 50s
    Else if top holder >70% of supply     → hold 25s
    Else (decentralized)                   → hold 60s
    Return 0 if any failure or no valid data.
    """
    try:
        client = get_random_client()
        resp = await client.get_token_largest_accounts(Pubkey.from_string(mint_address))
        await client.close()

        holders = resp.value
        if not holders:
            logger.warning(f"[❌LP CHECK] No holders found for {mint_address}.")
            return 0

        # Sum up total token supply among the fetched accounts:
        total_tokens = sum(h.ui_amount for h in holders if h.ui_amount)
        if total_tokens == 0:
            logger.warning(f"[❌LP CHECK] Total token supply zero for {mint_address}.")
            return 0

        top_holder = holders[0]
        top_owner = top_holder.address.to_string()
        top_amount = float(top_holder.ui_amount)
        top_pct = top_amount / total_tokens

        logger.info(f"[📊LP CHECK] {mint_address} top holder {top_owner} holds {top_pct:.2%}.")

        # If top owner isn't a known burn address → hold 50s
        if top_owner not in BURN_ADDRESSES:
            logger.info(f"[⚠️LP CHECK] {mint_address} top owner is not burn: {top_owner}. Hold 50s.")
            return 50

        # If top owner holds >70% → hold 25s
        if top_pct > 0.70:
            logger.warning(f"[⚠️LP CHECK] {mint_address} top holder >70% ({top_pct:.2%}). Hold 25s.")
            return 25

        # Otherwise, safe → hold 60s
        logger.info(f"[✅LP CHECK] {mint_address} LP ownership is decentralized. Hold 60s.")
        return 60

    except Exception as e:
        logger.error(f"[🚫LP CHECK] Error checking LP ownership for {mint_address}: {e}")
        return 0


async def passes_all_checks(mint_address: str) -> int:
    """
    Run all safety checks in sequence:
     1. is_token_rug()
     2. check_freeze_authority()
     3. check_lp_ownership()

    Returns:
      0  → fail/skip
      25 → buy + hold 25s
      50 → buy + hold 50s
      60 → buy + hold 60s
    """
    try:
        # 1) Rug/Honeypot check
        if await is_token_rug(mint_address):
            logger.info(f"[❌ALL CHECKS] {mint_address} is a rug.")
            return 0

        # 2) Freeze authority check
        freeze_ok = await check_freeze_authority(mint_address)
        if not freeze_ok:
            logger.info(f"[❌ALL CHECKS] {mint_address} has freeze authority.")
            return 0

        # 3) LP ownership distribution check
        hold_time = await check_lp_ownership(mint_address)
        if hold_time == 0:
            logger.info(f"[❌ALL CHECKS] {mint_address} failed LP ownership check.")
            return 0

        logger.info(f"[✅ALL CHECKS] {mint_address} passed all checks (hold {hold_time}s).")
        return hold_time

    except Exception as e:
        logger.error(f"[🚫ALL CHECKS] Unexpected error for {mint_address}: {e}")
        return 0