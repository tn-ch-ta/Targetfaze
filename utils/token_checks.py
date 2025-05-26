# utils/token_checks.py

import httpx
_original_async_init = httpx.AsyncClient.__init__
def _patched_async_init(self, *args, proxy=None, **kwargs):
    return _original_async_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_async_init

import asyncio
import logging
import aiohttp
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from spl.token._layouts import MINT_LAYOUT
from base64 import b64decode

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("token_checks")

# Constants
RPC_URL = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://lite-api.jup.ag/swap/v1/quote"


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


async def check_freeze_authority(mint_address: str) -> bool:
    """
    Returns True if SAFE (no freeze authority). Returns False if:
      - freeze authority is present, or
      - account not found, or
      - rate-limited (429), or
      - any other error.
    """
    try:
        client = AsyncClient(RPC_URL)
        mint_pubkey = Pubkey.from_string(mint_address)
        resp = await client.get_account_info(mint_pubkey)
        await client.close()

        # resp.value can be None if the account doesn't exist
        if resp.value is None:
            logger.info(f"[❌FREEZE CHECK] {mint_address} account not found (resp.value is None).")
            return False

        # resp.value.data is a list: [base64_data, encoding]
        account_data = resp.value.data
        if not account_data or len(account_data) < 2 or account_data[1] != "base64":
            logger.info(f"[❌FREEZE CHECK] {mint_address} has invalid account data format.")
            return False

        raw = b64decode(account_data[0])
        parsed = MINT_LAYOUT.parse(raw)

        # parsed.freeze_authority_option == 0 means "no freeze authority"
        if parsed.freeze_authority_option == 0:
            logger.info(f"[✅FREEZE CHECK] {mint_address} has no freeze authority (safe).")
            return True

        # If freeze_authority_option == 1, then freeze_authority is present
        freeze_auth_bytes = parsed.freeze_authority
        freeze_auth_pubkey = Pubkey.from_bytes(bytes(freeze_auth_bytes))
        logger.info(f"[❌FREEZE CHECK] {mint_address} has freeze authority: {freeze_auth_pubkey}")
        return False

    except Exception as e:
        # If the RPC returned a 429 or any other failure, catch it here.
        err_text = str(e)
        if "429" in err_text:
            logger.warning(f"[❌FREEZE CHECK] Rate-limited (429) checking {mint_address}.")
        else:
            logger.error(f"[🚫FREEZE CHECK] Unexpected error for {mint_address}: {e}")
        return False


async def check_insider_distribution(mint_address: str, max_pct: float = 10.0) -> bool:
    """
    Ensure no single wallet holds >= max_pct% of the supply.
    Returns True if safe, False otherwise.
    """
    try:
        client = AsyncClient(RPC_URL)
        mint_pubkey = Pubkey.from_string(mint_address)
        resp = await client.get_token_largest_accounts(mint_pubkey)
        await client.close()

        # resp.value can be None or empty
        accounts = getattr(resp, "value", None)
        if not accounts:
            logger.info(f"[⚠️INSIDER CHECK] {mint_address} has no holder data (maybe account not yet initialized).")
            return False

        # Sum up all reported balances
        total = sum(int(acc.amount) for acc in accounts)
        if total == 0:
            logger.info(f"[⚠️INSIDER CHECK] {mint_address} total supply is zero or no distribution info.")
            return False

        top_amount = int(accounts[0].amount)
        top_pct = (top_amount / total) * 100
        if top_pct >= max_pct:
            logger.info(f"[❌INSIDER CHECK] {mint_address} top holder {top_pct:.2f}% >= {max_pct}%.")
            return False

        logger.info(f"[✅INSIDER CHECK] {mint_address} passed (top holder {top_pct:.2f}%).")
        return True

    except Exception as e:
        err_text = str(e)
        if "429" in err_text:
            logger.warning(f"[❌INSIDER CHECK] Rate-limited (429) checking {mint_address}.")
        else:
            logger.error(f"[🚫INSIDER CHECK] Unexpected error for {mint_address}: {e}")
        return False


async def check_holder_diversity(
    mint_address: str, top_n: int = 10, max_pct: float = 70.0
) -> bool:
    """
    Ensure top_n holders together own < max_pct% of total supply.
    Returns True if safe, False otherwise.
    """
    try:
        client = AsyncClient(RPC_URL)
        mint_pubkey = Pubkey.from_string(mint_address)
        resp = await client.get_token_largest_accounts(mint_pubkey)
        await client.close()

        accounts = getattr(resp, "value", None)
        if not accounts:
            logger.info(f"[⚠️DIVERSITY CHECK] {mint_address} has no holder data.")
            return False

        total = sum(int(acc.amount) for acc in accounts)
        if total == 0 or len(accounts) < top_n:
            logger.info(f"[⚠️DIVERSITY CHECK] {mint_address} insufficient data or total zero.")
            return False

        top_sum = sum(int(acc.amount) for acc in accounts[:top_n])
        top_pct = (top_sum / total) * 100
        if top_pct >= max_pct:
            logger.info(f"[❌DIVERSITY CHECK] {mint_address} top {top_n} hold {top_pct:.2f}% >= {max_pct}%.")
            return False

        logger.info(f"[✅DIVERSITY CHECK] {mint_address} passed (top {top_n} hold {top_pct:.2f}%).")
        return True

    except Exception as e:
        err_text = str(e)
        if "429" in err_text:
            logger.warning(f"[❌DIVERSITY CHECK] Rate-limited (429) checking {mint_address}.")
        else:
            logger.error(f"[🚫DIVERSITY CHECK] Unexpected error for {mint_address}: {e}")
        return False


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
        freeze_safe = await check_freeze_authority(mint_address)
        insider_ok = await check_insider_distribution(mint_address)
        diversity_ok = await check_holder_diversity(mint_address)
        liquidity_ok = await check_liquidity(mint_address)

        if is_rug_flag:
            logger.info(f"[❌ALL CHECKS] {mint_address} failed: Rug detected.")
            return False
        if not freeze_safe:
            logger.info(f"[❌ALL CHECKS] {mint_address} failed: Freeze authority present.")
            return False
        if not insider_ok:
            logger.info(f"[❌ALL CHECKS] {mint_address} failed: Insider concentration too high.")
            return False
        if not diversity_ok:
            logger.info(f"[❌ALL CHECKS] {mint_address} failed: Holder diversity insufficient.")
            return False
        if not liquidity_ok:
            logger.info(f"[❌ALL CHECKS] {mint_address} failed: Insufficient liquidity.")
            return False

        logger.info(f"[✅ALL CHECKS] {mint_address} passed all checks.")
        return True
    except Exception as e:
        logger.error(f"[🚫ALL CHECKS] Error for {mint_address}: {e}")
        return False