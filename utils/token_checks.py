# utils/token_checks.py

import aiohttp
import asyncio
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

PUMP_TOKEN_API = "https://frontend-api-v3.pump.fun/coins/latest"  # Base URL; mint will be appended
RPC_URL = "https://api.mainnet-beta.solana.com"
SOLANA_CLIENT = AsyncClient(RPC_URL)

async def fetch_token_data(mint: str) -> dict:
    url = f"{PUMP_TOKEN_API}{mint}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                resp.raise_for_status()
                return await resp.json()
    except Exception as e:
        print(f"[ERROR] fetch_token_data({mint}): {e}")
        return {}

async def is_token_rug(mint: str) -> bool:
    data = await fetch_token_data(mint)
    # Pump.fun indicates honeypot or high taxes
    if data.get("isHoneypot") or data.get("buyTax", 0) > 10 or data.get("sellTax", 0) > 10:
        return True
    return False

async def check_insider_distribution(mint: str) -> bool:
    data = await fetch_token_data(mint)
    holders = data.get("holders_info", [])
    top5 = [h.get("percentage", 0) for h in holders[:5]]
    top_holder_pct = max(top5, default=0)
    return top_holder_pct < 10

async def check_freeze_authority(mint: str) -> bool:
    """
    Returns True if freeze authority EXISTS (i.e. risky),
    so invert for pass/fail logic elsewhere.
    """
    try:
        pubkey = Pubkey.from_string(mint)
        resp = await SOLANA_CLIENT.get_account_info(pubkey, encoding="jsonParsed")
        value = resp.value
        if value is None:
            print(f"[ERROR] No on-chain data for {mint}")
            return True  # treat missing data as risky
        parsed = value.data["parsed"]["info"]
        # If freezeAuthority is None → no freeze authority → safe
        has_authority = parsed.get("freezeAuthority") is not None
        return has_authority
    except Exception as e:
        print(f"[ERROR] check_freeze_authority({mint}): {e}")
        return True  # assume risky if uncertain

async def check_liquidity(mint: str, min_sol: float = 0.5) -> bool:
    data = await fetch_token_data(mint)
    try:
        liquidity = float(data.get("liquidity", 0))
        return liquidity >= min_sol
    except:
        return False

async def check_holder_diversity(mint: str) -> bool:
    data = await fetch_token_data(mint)
    holders = data.get("holders_info", [])
    if len(holders) < 10:
        return False
    top10 = sum(h.get("percentage", 0) for h in holders[:10])
    return top10 < 70

# Helper to gracefully close the AsyncClient on shutdown
async def close_solana_client():
    await SOLANA_CLIENT.close()

# Example aggregated function to run all checks
async def passes_all_checks(mint: str, min_liquidity: float = 0.5) -> bool:
    if await is_token_rug(mint):
        return False
    if await check_insider_distribution(mint) is False:
        return False
    if await check_freeze_authority(mint):
        return False
    if await check_liquidity(mint, min_liquidity) is False:
        return False
    if await check_holder_diversity(mint) is False:
        return False
    return True
