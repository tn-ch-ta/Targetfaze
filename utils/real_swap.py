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
import uuid
import logging

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.signature import Signature
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
        "slippageBps":               int(slippage * 150),
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
async def get_swap_transaction(quote_response: dict, user_pubkey: Pubkey, request_id: str) -> tuple[bytes, str]:
    """
    Given Jupiter’s quoteResponse, send to /swap to get the serialized VersionedTransaction + requestId.

    Returns:
        - swapTransaction (bytes)
        - requestId (str) for use in /trigger/v1/execute
    """
    
    log_bool_fields(quote_response)
    
    quote_clean = clean_none(quote_response)

    payload = {
        "quoteResponse": quote_clean,
        "userPublicKey": str(user_pubkey),
        "wrapUnwrapSOL": True,
        "useSharedAccounts": False,
        "usePriorityFee": False,
        "dynamicComputeUnitLimit": True,
        "dynamicSlippage": True,
        "simulateTx": False,
        "requestId": request_id,  # ✅ Attach it here
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
            status, url = resp.status, str(resp.url)
            print(f"[DEBUG] Swap HTTP status: {status}, URL: {url}")
            try:
                json_data = await resp.json()
            except Exception as e:
                text = await resp.text()
                raise Exception(f"[ERROR] Swap JSON parse failed: {e}\n{text}")

    print(f"[DEBUG] Swap API response:\n{json.dumps(json_data, indent=2)}")

    # Get and decode swapTransaction
    tx_raw = json_data.get("swapTransaction")
    if not tx_raw:
        raise Exception(f"Jupiter swap failed, no transaction returned: {json_data}")

    if isinstance(tx_raw, str):
        print(f"[DEBUG] swapTransaction is a Base64 string (len={len(tx_raw)})")
        try:
            tx_bytes = base64.b64decode(tx_raw)
        except Exception as e:
            snippet = tx_raw[:10] + ("..." if len(tx_raw) > 10 else "")
            raise Exception(f"[ERROR] base64.b64decode failed on swapTransaction (“{snippet}”): {e}")

    elif isinstance(tx_raw, list):
        print(f"[DEBUG] swapTransaction is a raw byte list (len={len(tx_raw)})")
        try:
            tx_bytes = bytes(tx_raw)
        except Exception as e:
            raise Exception(f"[ERROR] Converting swapTransaction list[int] → bytes failed: {e}")
    else:
        raise Exception(f"[ERROR] Unexpected swapTransaction format: {type(tx_raw)}")

    return tx_bytes, request_id



# Step 3: Send a signed, versioned transaction to Solana mainnet
# ──────────────────────────────────────────────────────────────────────────────
async def send_transaction(raw_tx_bytes: bytes, keypair: Keypair, request_id: str) -> str:
    try:
        print("[DEBUG] Step 1: Deserializing transaction bytes from Jupiter...")
        try:
            unsigned_tx: VersionedTransaction = VersionedTransaction.from_bytes(raw_tx_bytes)
            print("[DEBUG] Deserialization complete.")
            print(f"[DEBUG] Unsigned transaction:\n{unsigned_tx}")
        except Exception as e:
            print(f"[ERROR] Failed to Deserialize: {e}")
            return None
    
        print("\n[DEBUG] Step 2: Extracting MessageV0 from VersionedTransaction...")
        message: MessageV0 = unsigned_tx.message
        header = message.header
        num_required_sigs = header.num_required_signatures
        print(f"[DEBUG] num_required_signatures = {num_required_sigs}")
        print("[DEBUG] Extracted message:")
        print(message)

        print("\n[DEBUG] Step 3: Signing the message with keypair...")
        sig: Signature = keypair.sign_message(bytes(message))
        print(f"[DEBUG] ✅ Signature: {sig}")

        # Step 4: Encode signature in base64
        print("\n[DEBUG] Step 4: Encoding signature in base64...")
        base64_sig = base64.b64encode(bytes(sig)).decode()
        print(f"[DEBUG] Base64 Signature: {base64_sig}")
        
        # Step 5: Submit to Jupiter's execute_transaction
        print("\n[DEBUG] Step 5: Sending to Jupiter /trigger/v1/execute...")
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://lite-api.jup.ag/trigger/v1/execute",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                },
                json={
                    "requestId": request_id,  # Optional but recommended
                    "signedTransaction": base64_sig  # Signature only
                }
            ) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}: {await resp.text()}")

                resp_data = await resp.json()
                print("[DEBUG] Jupiter Response:")
                print(resp_data)

                if "error" in resp_data:
                    print(f"[ERROR] Jupiter returned error: {resp_data['error']}")
                    return None

                txid = resp_data.get("txid")
                if not txid:
                    print("[ERROR] No txid in response.")
                    return None

                print(f"[✅] Transaction sent! Txid: {txid}")
                return txid
    except Exception as e:
        print(f"[FATAL ERROR] send_transaction failed: {e}")
        return None
# ──────────────────────────────────────────────────────────────────────────────
# High-level helper to buy a token with real Jupiter swap
# ──────────────────────────────────────────────────────────────────────────────
async def buy_token_real(private_key: str, mint: str, sol_amount: float):
    print(f"[BUY] Buying {mint} for {sol_amount} SOL")
    kp = get_keypair_from_base58(private_key)
    lamports = int(sol_amount * 1e9)

    quote_response = await get_swap_route(SOL_MINT, mint, lamports)
    # Step 1.5: Generate request_id here and pass it forward
    request_id = str(uuid.uuid4())
    raw_tx_bytes, request_id = await get_swap_transaction(quote_response, kp.pubkey())

    try:
        txid = await send_transaction(raw_tx_bytes, kp, request_id)
        if not txid:
            raise Exception("Transaction failed or returned no txid.")
    except Exception as e:
        raise Exception(f"[ERROR] Final send_transaction (buy) failed: {e}")

    print(f"[BUY] Completed buy of {mint}, Transaction ID: {txid}")


# ──────────────────────────────────────────────────────────────────────────────
# High-level helper to sell (98% of balance) using real Jupiter swap
# ──────────────────────────────────────────────────────────────────────────────
async def sell_token_real(private_key: str, mint: str):
    from spl.token.instructions import get_associated_token_address
    from solana.rpc.commitment import Confirmed

    print(f"[SELL] Selling all of {mint}")
    kp = get_keypair_from_base58(private_key)

    # 1) Derive the ATA (associated token account) for this mint & user
    ata = get_associated_token_address(
        owner=kp.pubkey(), 
        mint=Pubkey.from_string(mint)
    )

    # 2) Fetch current token balance
    resp = await client.get_token_account_balance(ata, commitment=Confirmed)
    balance = int(resp.value.amount)
    print(f"[SELL] Token account {ata}, balance = {balance}")
    if balance == 0:
        print("[SELL] Nothing to sell.")
        return

    # 3) Compute 98% of balance (Jupiter will handle exact slippage deduction)
    sell_amount = int(balance * 0.98)
    if sell_amount == 0:
        print("[SELL] 98% of balance is 0, skipping.")
        return

    # 4) Get a quote: token → SOL (still quote for full amount)
    quote_response = await get_swap_route(mint, SOL_MINT, balance)
    # Step 1.5: Generate request_id here and pass it forward
    request_id = str(uuid.uuid4())
    raw_tx_bytes, request_id = await get_swap_transaction(quote_response, kp.pubkey())

    try:
        txid = await send_transaction(raw_tx_bytes, kp, request_id)
        if not txid:
            raise Exception("Transaction failed or returned no txid.")
    except Exception as e:
        raise Exception(f"[ERROR] Final send_transaction (sell) failed: {e}")
        
    print(f"[SELL] Completed sell of {mint}, Transaction ID: {txid}")


# ──────────────────────────────────────────────────────────────────────────────
# Helper to fetch a token account’s balance (in raw amount)
# ──────────────────────────────────────────────────────────────────────────────
async def get_token_balance(token_account: Pubkey) -> int:
    resp = await client.get_token_account_balance(token_account)
    amt  = int(resp.value.amount)
    print(f"[DEBUG] Token balance for {token_account}: {amt}")
    return amt