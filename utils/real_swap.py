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
import base64
import aiohttp
import json
import logging

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed

logger = logging.getLogger("real_swap")
logging.basicConfig(level=logging.INFO)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
RPC_URL            = "https://api.mainnet-beta.solana.com"
SOL_MINT           = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API  = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API   = "https://lite-api.jup.ag/swap/v1/swap"

# shared AsyncClient (re-used to query balances or sendRawTransaction)
client = AsyncClient(RPC_URL)


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

    try:
        kp = Keypair.from_secret_key(raw_bytes)
    except Exception as e:
        raise Exception(f"[ERROR] Keypair.from_secret_key failed: {e}")

    print(f"[DEBUG] Loaded Keypair, pubkey={kp.public_key}")
    return kp


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
# Step 2: Build a real transaction from the quoteResponse and decode into raw bytes
# ──────────────────────────────────────────────────────────────────────────────
async def get_swap_transaction(quote_response: dict, user_pubkey: Pubkey) -> bytes:
    """
    Given Jupiter’s quoteResponse, send to /swap to get the serialized VersionedTransaction.
    Jupiter may return the transaction as either:
      • a Base64-encoded string
      • a raw array of bytes (list[int])
    We decode whichever form we see into raw bytes.
    """
    # 1) Log boolean fields (debug)
    log_bool_fields(quote_response)

    # 2) Remove None values
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

    tx_raw = json_data.get("swapTransaction")
    if not tx_raw:
        raise Exception(f"Jupiter swap failed, no transaction returned: {json_data}")

    # Jupiter returns the transaction as a Base64-encoded string (“AQAAAAAA…”),
    # or as a list of ints. We decode accordingly:

    if isinstance(tx_raw, str):
        print(f"[DEBUG] swapTransaction is a Base64 string (len={len(tx_raw)})")
        try:
            return base64.b64decode(tx_raw)
        except Exception as e:
            snippet = tx_raw[:10] + ("..." if len(tx_raw) > 10 else "")
            raise Exception(f"[ERROR] base64.b64decode failed on swapTransaction (“{snippet}”): {e}")

    elif isinstance(tx_raw, list):
        print(f"[DEBUG] swapTransaction is a raw byte list (len={len(tx_raw)})")
        try:
            return bytes(tx_raw)
        except Exception as e:
            raise Exception(f"[ERROR] Converting swapTransaction list[int] → bytes failed: {e}")

    else:
        raise Exception(f"[ERROR] Unexpected swapTransaction format: {type(tx_raw)}")


# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPER: submit a fully-signed tx via JSON-RPC
# ──────────────────────────────────────────────────────────────────────────────
async def _send_raw_via_rpc(signed_tx_bytes: bytes) -> str:
    """
    Base64-encode `signed_tx_bytes` and invoke `sendTransaction` JSON-RPC.
    Returns the transaction signature on success.
    """
    tx_b64 = base64.b64encode(signed_tx_bytes).decode("utf-8")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            tx_b64,
            {
                "encoding": "base64",
                "skipPreflight": True,
                "preflightCommitment": "confirmed"
            }
        ]
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(RPC_URL, json=payload) as resp:
            resp_json = await resp.json()
            print(f"[DEBUG] sendTransaction response: {json.dumps(resp_json, indent=2)}")
            if resp_json.get("error"):
                raise Exception(f"sendTransaction failed: {resp_json['error']}")
            return resp_json["result"]


# Step 3: Send a signed, versioned transaction to Solana mainnet
# ──────────────────────────────────────────────────────────────────────────────
async def send_transaction(raw_tx_bytes: bytes, keypair: Keypair) -> str:
    """
    1) Deserialize Jupiter’s raw_tx_bytes into a solana-py VersionedTransaction.
    2) Serialize the signed tx and push via JSON-RPC (sendTransaction).
    """
    try:
        # ------------------------------------------------------------
        # DEBUG: Validate types
        # ------------------------------------------------------------
        print(f"[DEBUG] send_transaction called with:")
        print(f"         raw_tx_bytes type  = {type(raw_tx_bytes)}")
        print(f"         keypair      type  = {type(keypair)} / pubkey={keypair.public_key}")
        if not isinstance(raw_tx_bytes, (bytes, bytearray)):
            raise Exception(f"[ERROR] raw_tx_bytes is not bytes/bytearray!  Got: {type(raw_tx_bytes)}")

        # 1) Deserialize Jupiter’s bytes into a solana-py VersionedTransaction
        tx = VersionedTransaction.deserialize(raw_tx_bytes)

        # ------------------------------------------------------------
        # DEBUG: Inspect account_keys & existing signatures
        # ------------------------------------------------------------
        print(f"[DEBUG] Deserialized VersionedTransaction:")
        print(f"         message.account_keys (len={len(tx.message.account_keys)}):")
        for i, acct in enumerate(tx.message.account_keys):
            print(f"           slot {i:>2}: {acct}")
        print(f"         original signatures (len={len(tx.signatures)}):")
        for i, s in enumerate(tx.signatures):
            print(f"           slot {i:>2}: {s}")

        
        # DEBUG: Show signatures after signing
        # ------------------------------------------------------------
        print(f"[DEBUG] Signatures after calling tx.sign([...]):")
        for i, s in enumerate(tx.signatures):
            print(f"           slot {i:>2}: {s}")

        # 2) Serialize the fully signed transaction
        serialized_bytes = bytes(tx)
        print(f"[DEBUG] Signed transaction serialized size: {len(serialized_bytes)} bytes")

        # 3) Submit via JSON-RPC
        sig_str = await _send_raw_via_rpc(serialized_bytes)
        print(f"[TXN] Sent & confirmed: {sig_str}")
        return sig_str

    except Exception as e:
        raise Exception(f"[ERROR] Final send_transaction failed: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# High-level helper to buy a token with real Jupiter swap
# ──────────────────────────────────────────────────────────────────────────────
async def buy_token_real(private_key: str, mint: str, sol_amount: float):
    print(f"[BUY] Buying {mint} for {sol_amount} SOL")
    kp = get_keypair_from_base58(private_key)
    lamports = int(sol_amount * 1e9)

    quote_response = await get_swap_route(SOL_MINT, mint, lamports)
    raw_tx_bytes   = await get_swap_transaction(quote_response, kp.public_key)

    try:
        sig = await send_transaction(raw_tx_bytes, kp)
    except Exception as e:
        raise Exception(f"[ERROR] Final send_transaction (buy) failed: {e}")

    print(f"[BUY] Completed buy of {mint}, signature: {sig}")


# ──────────────────────────────────────────────────────────────────────────────
# High-level helper to sell (98% of balance) using real Jupiter swap
# ──────────────────────────────────────────────────────────────────────────────
async def sell_token_real(private_key: str, mint: str):
    from solana.system_program import SYSVAR_RENT_PUBKEY
    from spl.token.async_client import AsyncToken
    from solana.rpc.commitment import Confirmed

    print(f"[SELL] Selling all of {mint}")
    kp = get_keypair_from_base58(private_key)

    # 1) Derive the ATA (associated token account) for this mint & user
    ata = AsyncToken.get_associated_token_address(
        owner=kp.public_key, 
        mint=PublicKey(mint)
    )

    # 2) Fetch current token balance
    resp = await client.get_token_account_balance(ata, commitment=Confirmed)
    balance = int(resp.value.amount)
    print(f"[SELL] Token account {ata}, balance = {balance}")
    if balance == 0:
        print("[SELL] Nothing to sell.")
        return

    # 3) Compute 98% of balance (but we still quote for full `balance`; Jupiter will deduct fee)
    sell_amount = int(balance * 0.98)
    if sell_amount == 0:
        print("[SELL] 98% of balance is 0, skipping.")
        return

    # 4) Get a quote: token → SOL
    quote_response = await get_swap_route(mint, SOL_MINT, balance)
    raw_tx_bytes   = await get_swap_transaction(quote_response, kp.public_key)

    try:
        sig = await send_transaction(raw_tx_bytes, kp)
    except Exception as e:
        raise Exception(f"[ERROR] Final send_transaction (sell) failed: {e}")

    print(f"[SELL] Completed sell of {mint}, signature: {sig}")


# ──────────────────────────────────────────────────────────────────────────────
# Helper to fetch a token account’s balance (in raw amount)
# ──────────────────────────────────────────────────────────────────────────────
async def get_token_balance(token_account: Pubkey) -> int:
    resp = await client.get_token_account_balance(token_account)
    amt  = int(resp.value.amount)
    print(f"[DEBUG] Token balance for {token_account}: {amt}")
    return amt