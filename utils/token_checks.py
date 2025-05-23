# utils/token_checks.py

import httpx
_original_async_init = httpx.AsyncClient.__init__
def _patched_async_init(self, *args, proxy=None, **kwargs):
    return _original_async_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_async_init

import aiohttp
import asyncio
from solders.pubkey import Pubkey

# Constants
RPC_URL = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"

# Helper to send raw JSON-RPC to Solana
async def _rpc(method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with aiohttp.ClientSession() as sess:
        async with sess.post(RPC_URL, json=payload, timeout=10) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("result")

async def is_token_rug(mint: str) -> bool:
    """
    Honeypot/rug detection via Jupiter quote back to SOL.
    If no direct route or outAmount==0, treat as rug.
    """
    params = {
        "inputMint": mint,
        "outputMint": SOL_MINT,
        "amount": 1000,        # tiny amount
        "slippage": 1,
        "onlyDirectRoutes": True
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(JUPITER_QUOTE_API, params=params, timeout=5) as resp:
                data = await resp.json()
        routes = data.get("data", [])
        return not routes or int(routes[0].get("outAmount", 0)) == 0
    except Exception:
        return True  # assume rug on error

async def check_freeze_authority(mint: str) -> bool:
    """
    Returns True if SAFE (no freeze authority), False if risky.
    """
    try:
        # getAccountInfo with jsonParsed
        result = await _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        info = result["value"]
        if info is None:
            return False
        parsed = info["data"]["parsed"]["info"]
        return parsed.get("freezeAuthority") is None
    except Exception:
        return False

async def check_insider_distribution(mint: str, max_pct: float = 10.0) -> bool:
    """
    Ensure no single wallet holds >= max_pct% of total supply.
    """
    try:
        resp = await _rpc("getTokenLargestAccounts", [mint])
        accounts = resp["value"]
        total = sum(int(a["amount"]) for a in accounts)
        if total == 0:
            return False
        top = int(accounts[0]["amount"])
        return (top / total * 100) < max_pct
    except Exception:
        return False

async def check_holder_diversity(mint: str, top_n: int = 10, max_pct: float = 70.0) -> bool:
    """
    Ensure top_n holders together own < max_pct% of supply.
    """
    try:
        resp = await _rpc("getTokenLargestAccounts", [mint])
        accounts = resp["value"]
        total = sum(int(a["amount"]) for a in accounts)
        if total == 0 or len(accounts) < top_n:
            return False
        top_sum = sum(int(a["amount"]) for a in accounts[:top_n])
        return (top_sum / total * 100) < max_pct
    except Exception:
        return False

async def check_liquidity(mint: str, min_sol: float = 0.5) -> bool:
    """
    Check if at least min_sol SOL liquidity exists (via Jupiter quote).
    """
    lam = int(min_sol * 1e9)
    params = {
        "inputMint": SOL_MINT,
        "outputMint": mint,
        "amount": lam,
        "slippage": 1,
        "onlyDirectRoutes": True
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(JUPITER_QUOTE_API, params=params, timeout=5) as resp:
                data = await resp.json()
        routes = data.get("data", [])
        if not routes:
            return False
        out_amount = int(routes[0].get("outAmount", 0))
        return out_amount >= lam
    except Exception:
        return False

async def passes_all_checks(mint: str) -> bool:
    """
    Run all safety checks in parallel and combine results.
    """
    is_rug, freeze_safe, insider_ok, diversity_ok, liquidity_ok = await asyncio.gather(
        is_token_rug(mint),
        check_freeze_authority(mint),
        check_insider_distribution(mint),
        check_holder_diversity(mint),
        check_liquidity(mint),
    )
    # Only proceed if not rug, freeze_safe, and all others True
    return (not is_rug) and freeze_safe and insider_ok and diversity_ok and liquidity_ok
