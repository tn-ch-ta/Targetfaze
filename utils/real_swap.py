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
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.providers.async_http import AsyncHTTPProvider
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
import json
import logging

# ──────────────────────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────────────────────
logger = logging.getLogger("real_swap")
logging.basicConfig(level=logging.INFO)

# ──────────────────────────────────────────────────────────────────────────────
# Helper to drop None values from nested dict/list
# ──────────────────────────────────────────────────────────────────────────────
def clean_none(obj):
    if isinstance(obj, dict):
        return {k: clean_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [clean_none(v) for v in obj]
    return obj

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
# Constants
# ──────────────────────────────────────────────────────────────────────────────
RPC_URL            = "https://api.mainnet-beta.solana.com"
SOL_MINT           = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API  = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API   = "https://lite-api.jup.ag/swap/v1/swap"

# Shared AsyncClient (re-used for all transactions)
client = AsyncClient(RPC_URL)
client._provider = AsyncHTTPProvider(RPC_URL, timeout=30)

# ──────────────────────────────────────────────────────────────────────────────
# Utility: ensure a string is valid Base58 before decoding
# ──────────────────────────────────────────────────────────────────────────────
def ensure_valid_base58(s: str):
    """
    Raise an Exception if `s` is not valid Base58.
    This catches invalid characters like 'O', '0', 'I', 'l', etc.
    """
    try:
        # Attempt to decode; we don't actually need the bytes here, just validation
        _ = base58.b58decode(s)
    except Exception as e:
        # Show only the first 10 chars in logs to avoid printing massive strings
        snippet = s[:10] + ("..." if len(s) > 10 else "")
        raise Exception(f"Not valid base58 (“{snippet}”): {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Convert a base58-encoded private key into a Keypair, with validation
# ──────────────────────────────────────────────────────────────────────────────
def get_keypair_from_base58(private_key: str) -> Keypair:
    print(f"[DEBUG] → Received private_key (len={len(private_key)}): {private_key[:10]}…")
    # 1) Validate base58:
    ensure_valid_base58(private_key)
    # 2) Decode into bytes and build Keypair
    try:
        raw_bytes = base58.b58decode(private_key)
    except Exception as e:
        # This should never happen if ensure_valid_base58 passed, but guard anyway
        raise Exception(f"[ERROR] base58.b58decode failed on private_key: {e}")
    try:
        kp = Keypair.from_bytes(raw_bytes)
    except Exception as e:
        raise Exception(f"[ERROR] Keypair.from_bytes failed: {e}")
    print(f"[DEBUG] Loaded Keypair, pubkey={kp.pubkey()}")
    return kp

# ──────────────────────────────────────────────────────────────────────────────
# Fetch a Jupiter quote (“routePlan”) for swapping `amount` of input_mint → output_mint
# ──────────────────────────────────────────────────────────────────────────────
async def get_swap_route(input_mint: str, output_mint: str, amount: int, slippage: float = 1.0) -> dict:
    print(f"[DEBUG] Requesting quote: input={input_mint}, output={output_mint}, amount={amount}")
    params = {
        "inputMint":                 input_mint,
        "outputMint":                output_mint,
        "amount":                    amount,
        "slippageBps":               int(slippage * 100),
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
# Build a real transaction from the quoteResponse and send it
# ──────────────────────────────────────────────────────────────────────────────
async def get_swap_transaction(quote_response: dict, user_pubkey: Pubkey) -> bytes:
    # 1) Log boolean fields for debugging
    log_bool_fields(quote_response)

    # 2) Clean out any None values in the quoteResponse
    quote_clean = clean_none(quote_response)

    payload = {
        "quoteResponse": quote_clean,
        "userPublicKey": str(user_pubkey),
        "wrapUnwrapSOL": True,
        "dynamicComputeUnitLimit": True,
        "dynamicSlippage": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": 1_000_000,
                "priorityLevel": "veryHigh"
            }
        }
    }

    print("[DEBUG] Final swap payload:")
    print(json.dumps(payload, indent=2))

    async with aiohttp.ClientSession() as session:
        async with session.post(JUPITER_SWAP_API, json=payload) as resp:
            status, url = resp.status, resp.url
            print(f"[DEBUG] Swap HTTP status: {status}, URL: {url}")
            try:
                json_data = await resp.json()
            except Exception as e:
                text = await resp.text()
                raise Exception(f"[ERROR] Swap JSON parse failed: {e}\n{text}")

    print(f"[DEBUG] Swap API response:\n{json.dumps(json_data, indent=2)}")

    tx_b58 = json_data.get("swapTransaction")
    if not tx_b58:
        raise Exception(f"Jupiter swap failed, no transaction returned: {json_data}")

    # 3) Validate that the returned string is valid base58
    print(f"[DEBUG] Received swapTransaction base58: {tx_b58[:20]}… (len={len(tx_b58)})")
    try:
        ensure_valid_base58(tx_b58)
    except Exception as e:
        raise Exception(f"[ERROR] swapTransaction is not valid base58: {e}")

    # 4) Finally decode into bytes and return
    try:
        raw_bytes = base58.b58decode(tx_b58)
    except Exception as e:
        raise Exception(f"[ERROR] base58.b58decode failed on swapTransaction: {e}")

    return raw_bytes

# ──────────────────────────────────────────────────────────────────────────────
# Send a signed, versioned transaction to Solana mainnet
# ──────────────────────────────────────────────────────────────────────────────
async def send_transaction(raw_tx_bytes: bytes, keypair: Keypair) -> str:
    tx = VersionedTransaction.deserialize(raw_tx_bytes)
    tx.sign([keypair])
    serialized = tx.serialize()
    print(f"[DEBUG] Signed transaction size: {len(serialized)} bytes")

    sig_resp = await client.send_raw_transaction(
        serialized,
        opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
    )
    sig = sig_resp.value
    print(f"[TXN] Sent:      {sig}")
    await client.confirm_transaction(sig, commitment=Confirmed)
    print(f"[TXN] Confirmed: {sig}")
    return sig

# ──────────────────────────────────────────────────────────────────────────────
# High‐level helper to buy a token with real Jupiter swap
# ──────────────────────────────────────────────────────────────────────────────
async def buy_token_real(private_key: str, mint: str, sol_amount: float):
    print(f"[BUY] Buying {mint} for {sol_amount} SOL")
    # 1) Validate and load Keypair
    kp = get_keypair_from_base58(private_key)

    # 2) Compute lamports
    lamports = int(sol_amount * 1e9)

    # 3) Fetch a quote from Jupiter
    quote_response = await get_swap_route(SOL_MINT, mint, lamports)

    # 4) Build the raw transaction
    tx_bytes       = await get_swap_transaction(quote_response, kp.pubkey())

    # 5) Send it on‐chain
    try:
        sig            = await send_transaction(tx_bytes, kp)
    except Exception as e:
        raise Exception(f"[ERROR] Final send_transaction failed: {e}")

    print(f"[BUY] Completed buy of {mint}, signature: {sig}")

# ──────────────────────────────────────────────────────────────────────────────
# High‐level helper to sell (98% of balance) using real Jupiter swap
# ──────────────────────────────────────────────────────────────────────────────
async def sell_token_real(private_key: str, mint: str):
    from spl.token.instructions import get_associated_token_address

    print(f"[SELL] Selling all of {mint}")
    # 1) Validate and load Keypair
    kp = get_keypair_from_base58(private_key)

    # 2) Derive the ATA (associated token account) for this mint & user
    try:
        ata = get_associated_token_address(kp.pubkey(), Pubkey.from_string(mint))
    except Exception as e:
        raise Exception(f"[ERROR] Could not derive ATA for {mint}: {e}")

    # 3) Fetch current token balance
    balance = await get_token_balance(ata)
    print(f"[SELL] Token account {ata}, balance = {balance}")
    if balance == 0:
        print("[SELL] Nothing to sell.")
        return

    # 4) Compute 98% of the balance
    sell_amount = int(balance * 0.98)
    if sell_amount == 0:
        print("[SELL] 98% of balance is 0, skipping.")
        return

    # 5) Get a quote: token → SOL
    quote_response = await get_swap_route(mint, SOL_MINT, balance)

    # 6) Build the transaction
    tx_bytes       = await get_swap_transaction(quote_response, kp.pubkey())

    # 7) Send it
    try:
        sig = await send_transaction(tx_bytes, kp)
    except Exception as e:
        raise Exception(f"[ERROR] Final send_transaction (sell) failed: {e}")

    print(f"[SELL] Completed sell of {mint}, signature: {sig}")

# ──────────────────────────────────────────────────────────────────────────────
# Helper to fetch a token account’s balance (in raw amount, not UI decimal)
# ──────────────────────────────────────────────────────────────────────────────
async def get_token_balance(token_account: Pubkey) -> int:
    resp = await client.get_token_account_balance(token_account)
    amt  = int(resp.value.amount)
    print(f"[DEBUG] Token balance for {token_account}: {amt}")
    return amt