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
DEXSCREENER_API = "https://api.dexscreener.com/token-pairs/v1/solana/{}"

# ── RPC Client Selector ───────────────────────────────────────────────────────
def get_random_client() -> AsyncClient:
    return AsyncClient(random.choice(SOLANA_RPC_URLS))

# ── Combined RugCheck-based Token Validation ──────────────────────────────────
async def passes_all_checks(mint_address: str) -> tuple | None:
    """
    Check token against RugCheck + DexScreener:
    - freezeAuthority and mintAuthority == null
    - totalStableLiquidity > 3000
    - score_normalised < 17
    - totalHolders > 22
    - 2nd holder pct < 3.4%
    - Has website or Twitter listed on DexScreener
    """
    url = RUGCHECK_API.format(mint_address)
    try:
        async with aiohttp.ClientSession() as session:
            # 1️⃣ RugCheck request
            async with session.get(url, timeout=7) as resp:
                data = await resp.json()

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
                if second_pct >= 3.4:
                    logger.warning(f"[❌TOP HOLDERS] {mint_address} 2nd holder pct={second_pct:.4f}% >= 3.4% — FAIL")
                    return None
                logger.info(f"[✅TOP HOLDERS] {mint_address} 2nd holder pct={second_pct:.4f}% < 3.4% — PASS")
            else:
                logger.warning(f"[❌TOP HOLDERS] {mint_address} has fewer than 2 holders — FAIL")
                return None

            # 2️⃣ DexScreener request for website/social check
            dex_url = DEXSCREENER_API.format(mint_address)
            async with session.get(dex_url, timeout=7) as dex_resp:
                dex_data = await dex_resp.json()

            has_website = False
            has_twitter = False
            website_url = None
            twitter_url = None

            if isinstance(dex_data, list) and len(dex_data) > 0:
                info = dex_data[0].get("info", {})
                websites = info.get("websites", [])
                socials = info.get("socials", [])

                if websites and isinstance(websites, list):
                    website_url = websites[0].get("url")
                    has_website = True

                if socials and isinstance(socials, list):
                    for social in socials:
                        if social.get("type") == "twitter":
                            twitter_url = social.get("url")
                            has_twitter = True
                            break

            if not has_website and not has_twitter:
                logger.warning(f"[❌DEX INFO] {mint_address} has no website or twitter listed — FAIL")
                return None


            # All checks passed
            logger.info(f"[✅ALL CHECKS] {mint_address} passed RugCheck + DexScreener checks.")
            return total_liquidity, score_norm, total_holders, second_pct, website_url, twitter_url

    except Exception as e:
        logger.error(f"[🚫ALL CHECKS] Error for {mint_address}: {e}")
        return None