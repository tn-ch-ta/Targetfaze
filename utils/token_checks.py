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

RUGCHECK_API = "https://api.rugcheck.xyz/v1/tokens/{}/report"

# ── RPC Client Selector ───────────────────────────────────────────────────────
def get_random_client() -> AsyncClient:
    return AsyncClient(random.choice(SOLANA_RPC_URLS))

# ── Combined RugCheck-based Token Validation ──────────────────────────────────
async def passes_all_checks(mint_address: str) -> tuple | None:
    """
    Check token against RugCheck for:
    - freezeAuthority and mintAuthority == null
    - totalStableLiquidity > 3000
    - score_normalised < 17
    - totalHolders > 22

    Returns:
        (liquidity, score_norm, total_holders) if all checks pass, else None.
    """
    url = RUGCHECK_API.format(mint_address)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=7) as resp:
                data = await resp.json()

        # Extract relevant fields from RugCheck response
        freeze_auth = data.get("freezeAuthority")
        mint_auth = data.get("mintAuthority")
        total_liquidity = float(data.get("totalStableLiquidity", 0))
        score_norm = float(data.get("score_normalised", 999))
        total_holders = int(data.get("totalHolders", -1))
        top_holders = data.get("topHolders", [])

        # Freeze & Mint Authority check
        if freeze_auth is not None or mint_auth is not None:
            logger.warning(f"[❌AUTH CHECK] {mint_address} freezeAuthority={freeze_auth}, mintAuthority={mint_auth}")
            return None
        logger.info(f"[✅AUTH CHECK] {mint_address} has no freeze or mint authority.")

        # Liquidity check
        if total_liquidity <= 3000:
            logger.warning(f"[❌LIQUIDITY CHECK] {mint_address} liquidity={total_liquidity:.2f} <= 3000.")
            return None
        logger.info(f"[✅LIQUIDITY CHECK] {mint_address} liquidity={total_liquidity:.2f} > 3000.")

        # Score & Holder check
        if score_norm >= 17 or total_holders <= 22:
            logger.warning(f"[❌RUGCHECK] {mint_address} score={score_norm:.2f}, holders={total_holders} — FAIL")
            return None
        logger.info(f"[✅RUGCHECK] {mint_address} score={score_norm:.2f}, holders={total_holders} — PASS")
        
        # Top holders check
        if len(top_holders) >= 2:
            second_pct = float(top_holders[1].get("pct", 100))
            if second_pct >= 3.0:
                logger.warning(f"[❌TOP HOLDERS] {mint_address} 2nd holder pct={second_pct:.4f}% >= 3.0% — FAIL")
                return None
            logger.info(f"[✅TOP HOLDERS] {mint_address} 2nd holder pct={second_pct:.4f}% < 3.0% — PASS")
        else:
            logger.warning(f"[❌TOP HOLDERS] {mint_address} has fewer than 2 holders — FAIL")
            return None

        logger.info(f"[✅ALL CHECKS] {mint_address} passed all RugCheck-based checks.")
        return total_liquidity, score_norm, total_holders, second_pct

    except Exception as e:
        logger.error(f"[🚫ALL CHECKS] Error for {mint_address}: {e}")
        return None