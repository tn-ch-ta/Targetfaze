# utils/real_swap.py

# ──────────────────────────────────────────────────────────────────────────────
# Monkey-patch httpx.AsyncClient to swallow the `proxy` kwarg so solana-py works
import httpx
_original_async_init = httpx.AsyncClient.__init__
def _patched_async_init(self, *args, proxy=None, **kwargs):
    return _original_async_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_async_init
# ──────────────────────────────────────────────────────────────────────────────

import base58
import aiohttp
import asyncio
import json
import logging
import time
import random

from asyncio import sleep
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Commitment, Confirmed, Finalized, Processed
from utils.solanatracker import SolanaTracker

logger = logging.getLogger("real_swap")
logging.basicConfig(level=logging.INFO)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
SOLANA_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.g.alchemy.com/v2/NHAveomHS7q-QGj2ddOa86_QzVi9QzeY",
    "https://solana-mainnet.g.alchemy.com/v2/D6p4-dGHuCfO42nBFTPzJdpWBN9vUlsz"
]
RPC_URL            = random.choice(SOLANA_RPC_URLS)
SOL_MINT           = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API  = "https://lite-api.jup.ag/swap/v1/quote"

# Create ONE shared AsyncClient on import — with randomized choice
client: AsyncClient = AsyncClient(random.choice(SOLANA_RPC_URLS))

# ──────────────────────────────────────────────────────────────────────────────
# Helper: drop None values from nested dict/list
# ──────────────────────────────────────────────────────────────────────────────
def clean_none(obj):
    if isinstance(obj, dict):
        return {k: clean_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [clean_none(v) for v in obj]
    return obj

# ──────────────────────────────────────────────────────────────────────────────
# Helper: print any bool fields (for debugging Jupiter quote payload)
# ──────────────────────────────────────────────────────────────────────────────
def log_bool_fields(obj, path="root"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, bool):
                print(f"[DEBUG] ⚠️ Boolean field: {path}.{k} = {v}")
            log_bool_fields(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            log_bool_fields(v, f"{path}[{i}]")


# ──────────────────────────────────────────────────────────────────────────────
# Convert a Base58-encoded private key into a Solana-py Keypair
# ──────────────────────────────────────────────────────────────────────────────
def get_keypair_from_base58(private_key: str) -> Keypair:
    """
    Expects a Base58-encoded 64-byte (secret key) string.
    """
    try:
        raw_bytes = base58.b58decode(private_key)
    except Exception as e:
        raise Exception(f"[ERROR] base58.b58decode failed on private_key: {e}")

    if len(raw_bytes) != 64:
        raise ValueError(f"[ERROR] Decoded key must be 64 bytes, got {len(raw_bytes)}")

    try:
        keypair = Keypair.from_bytes(raw_bytes)
    except Exception as e:
        raise Exception(f"[ERROR] Keypair.from_bytes failed: {e}")

    print(f"[DEBUG] Loaded Keypair, pubkey={keypair.pubkey()}")
    return keypair


# ──────────────────────────────────────────────────────────────────────────────
# Step 1: Fetch a Jupiter quote (“routePlan”) for swapping `amount` of input_mint → output_mint
# ──────────────────────────────────────────────────────────────────────────────
async def get_swap_route(input_mint: str, output_mint: str, amount: int, slippage: float = 1.0) -> dict:
    """
    Returns the full JSON response from Jupiter’s /quote endpoint.
    Must contain "routePlan" to be valid.
    """
    print(f"[DEBUG] Requesting quote: input={input_mint}, output={output_mint}, amount={amount}")
    params = {
        "inputMint":                 input_mint,
        "outputMint":                output_mint,
        "amount":                    amount,
        "slippageBps":               int(slippage * 250),
        "onlyDirectRoutes":          "false",
        "restrictIntermediateTokens": "true",
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(JUPITER_QUOTE_API, params=params) as resp:
            try:
                json_data = await resp.json()
            except Exception as e:
                text = await resp.text()
                raise Exception(f"[ERROR] Failed to parse JSON from Jupiter quote: {e}\n→ {text}")

    print(f"[DEBUG] Quote response:\n{json.dumps(json_data, indent=2)}")
    if not isinstance(json_data, dict) or "routePlan" not in json_data:
        raise Exception(f"No valid quote returned from Jupiter: {json_data}")

    return json_data


# ──────────────────────────────────────────────────────────────────────────────
# High-level swap helpers
# ──────────────────────────────────────────────────────────────────────────────
async def buy_token_real(
    private_key_b58: str,
    mint: str,
    sol_amount: float,
    slippage_pct: float = 2.5,
    priority_fee_sol: float = 0.00005,
) -> str:
    """
    Buys `sol_amount` SOL → `mint` using Jupiter for pricing, SolanaTracker for tx.
    Returns the txid.
    """
    kp = get_keypair_from_base58(private_key_b58)
    payer = str(kp.pubkey())
    lamports = int(sol_amount * 1e9)

    # 1) Get Jupiter quote (for logging & sanity check)
    quote = await get_swap_route(SOL_MINT, mint, lamports, slippage_pct)
    out_amt = int(quote["outAmount"])
    logger.info(f"[QUOTE] {sol_amount} SOL → {out_amt/1e9:.9f} {mint} (slippage {slippage_pct}%)")

    # 2) Initialize SolanaTracker on your chosen RPC
    tracker = SolanaTracker(kp, RPC_URL)

    # 3) Build swap instructions via SolanaTracker
    swap_resp = await tracker.get_swap_instructions(
        from_token=SOL_MINT,
        to_token=mint,
        from_amount=sol_amount,
        slippage=slippage_pct,
        payer=payer,
        priority_fee=priority_fee_sol,
    )

    # 4) Customize send & confirm behavior
    options = {
        "send_options": {
            "skip_preflight": True,
            "max_retries":    3,
        },
        "confirmation_retries":       20,
        "confirmation_retry_timeout": 1000,  # ms
        "last_valid_block_height_buffer": 150,
        "commitment":                 "processed",
        "resend_interval":            500,   # ms
        "confirmation_check_interval":200,   # ms
        "skip_confirmation_check":    False,
    }

    # 5) Perform the swap
    start = time.time()
    try:
        txid = await tracker.perform_swap(swap_resp, options=options)
    except Exception as e:
        if "6002" in str(e):
            raise Exception("Swap failed: Exceeded slippage tolerance (error 6002).")
        raise  # re-raise all other exceptions
    elapsed = time.time() - start

    logger.info(f"[BUY] Completed: {txid} in {elapsed:.2f}s")
    return txid

async def sell_token_real(
    private_key_b58: str,
    mint: str,
    slippage_pct: float = 2.5,
    priority_fee_sol: float = 0.00005,
) -> str:
    """
    Sells ~98% of your token balance back to SOL.
    """
    from spl.token.instructions import get_associated_token_address

    kp = get_keypair_from_base58(private_key_b58)
    payer = str(kp.pubkey())

    # 1) Derive your ATA & check balance
    ata = get_associated_token_address(kp.pubkey(), Pubkey.from_string(mint))
    resp = await client.get_token_account_balance(ata, commitment=Confirmed)
    balance = int(resp.value.amount)
    if balance == 0:
        raise RuntimeError("No tokens to sell.")
    # RIGHT ➡️ float token units
    sell_amt = int(balance * 0.98)  # ✅ Correct: whole number in raw token units

    # 2) Quote (optional logging)
    quote = await get_swap_route(mint, SOL_MINT, sell_amt, slippage_pct)
    out_amt = int(quote["outAmount"])  # ← convert here
    logger.info(f"[QUOTE] {balance/1e9:.9f} {mint} → {out_amt/1e9:.9f} SOL (slippage {slippage_pct}%)")

    # 3) Tracker setup & swap instructions
    tracker = SolanaTracker(kp, RPC_URL)
    swap_resp = await tracker.get_swap_instructions(
        mint,             # input_mint
        SOL_MINT,         # output_mint
        float(sell_amt),  # amount (as float)
        slippage_pct,     # slippage
        payer,            # payer
        priority_fee_sol, # priority_fee
        False             # force_legacy
    )

    # 4) Same options as buy
    options = {
        "send_options": {
            "skip_preflight": True,
            "max_retries":    3,
        },
        "confirmation_retries":       20,
        "confirmation_retry_timeout": 1000,
        "last_valid_block_height_buffer": 150,
        "commitment":                 "processed",
        "resend_interval":            500,
        "confirmation_check_interval":200,
        "skip_confirmation_check":    False,
    }

    start = time.time()
    try:
        txid = await tracker.perform_swap(swap_resp, options=options)
    except Exception as e:
        if "6002" in str(e):
            raise Exception("Swap failed: Exceeded slippage tolerance (error 6002).")
        raise
    elapsed = time.time() - start

    logger.info(f"[SELL] Completed: {txid} in {elapsed:.2f}s")
    return txid
