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
import asyncio
import json
import logging
import time
import random

from asyncio import sleep
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
SOLANA_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.g.alchemy.com/v2/NHAveomHS7q-QGj2ddOa86_QzVi9QzeY",
    "https://solana-mainnet.g.alchemy.com/v2/D6p4-dGHuCfO42nBFTPzJdpWBN9vUlsz",
    "https://solana-rpc.publicnode.com",
    "https://solana.drpc.org"
]
SOL_MINT           = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API  = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API   = "https://lite-api.jup.ag/swap/v1/swap"

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
async def get_swap_transaction(quote_response: dict, user_pubkey: Pubkey) -> bytes:
    """
    Given Jupiter’s quoteResponse, send to /swap to get the serialized VersionedTransaction + requestId.

    Returns:
        - swapTransaction (bytes)
    """
    
    log_bool_fields(quote_response)
    
    quote_clean = clean_none(quote_response)

    payload = {
        "quoteResponse": quote_clean,
        "userPublicKey": str(user_pubkey),
        "wrapUnwrapSOL": True,
        "useSharedAccounts": False,
        "usePriorityFee": True,  # Enable use of priority fees
        "dynamicSlippage": False,
        "simulateTx": False,
        "prioritizationFeeLamports": "auto", # Let Jupiter handle it
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
    last_valid = json_data.get("lastValidBlockHeight")
    if tx_raw is None or last_valid is None:
        raise Exception(f"Jupiter swap failed or Malformed swap response: {json_data}")

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

    return tx_bytes, last_valid



# Step 3: Send a signed, versioned transaction to Solana mainnet
# ──────────────────────────────────────────────────────────────────────────────
async def send_transaction(raw_tx_bytes: bytes, keypair: Keypair) -> str:
    try:
        print("[DEBUG] Step 1: Deserializing transaction bytes from Jupiter...")
        try:
            unsigned_tx: VersionedTransaction = VersionedTransaction.from_bytes(raw_tx_bytes)
            print("[DEBUG] Deserialization complete.")
            print(f"[DEBUG] Unsigned transaction:\n{unsigned_tx}")
        except Exception as e:
            print(f"[ERROR] Failed to Deserialize: {e}")
            return None
            
        # 1) Refresh the blockhash right before signing
        latest = await client.get_latest_blockhash(commitment=Confirmed)
        blockhash = latest.value.blockhash
        last_valid = latest.value.last_valid_block_height
    
        print("\n[DEBUG] Step 2: Extracting MessageV0 from VersionedTransaction...")
        message: MessageV0 = unsigned_tx.message
        header = message.header
        
        # Extract existing message fields
        account_keys = message.account_keys
        instructions = message.instructions
        address_table_lookups = message.address_table_lookups
        new_message = MessageV0(
            header=header,
            recent_blockhash=blockhash,  # ✅ replace the blockhash here
            account_keys=account_keys,
            instructions=instructions,
            address_table_lookups=address_table_lookups,
        )
        print("[DEBUG] Rebuilt message:")
        print(new_message)

        print("\n[DEBUG] Step 3: Signing the message with keypair...")
        sig: Signature = keypair.sign_message(bytes(new_message))
        print(f"[DEBUG] ✅ Signature: {sig}")
        
        print("\n[DEBUG] Step 4: Construct back into VersionedTransaction...")
        try:
            signed_tx = VersionedTransaction.populate(new_message, [sig])
            print("[DEBUG] Construction complete.")
            print(f"[DEBUG] Signed Transaction:\n{signed_tx}")
        except Exception as e:
            print(f"[ERROR] Failed to Construct: {e}")
            return None


        print("\n[DEBUG] Step 5: Serializing Signed Tx to bytes...")
        serialized_bytes = bytes(signed_tx)
        print(f"[DEBUG] ✅ Serialized bytes.")
        
        if not serialized_bytes.startswith(b'\x01'):
            raise Exception("Serialized transaction may be invalid (wrong version prefix).")
        
        # Step 6: Submit to Solana
        print("\n[DEBUG] Step 6: Sending to Solana")
        
        try:
            resp = await client.send_raw_transaction(
                serialized_bytes,
                opts=TxOpts(skip_preflight=True, preflight_commitment="processed" , last_valid_block_height=last_valid)
            )
            txid = resp.value if hasattr(resp, "value") else resp
            print(f"[TXN] Sent: {txid}")
        except Exception as e:
            raise Exception(f"[ERROR] send_raw_transaction RPC error: {e}")

        # Step 7: Confirm the transaction
        print("\n[DEBUG] Step 7: Confirming the TXN")
        poll_interval = 0.5   # seconds between retries
        timeout       = 90    # total timeout in seconds
        start         = time.time()

        while time.time() - start < timeout:
            try:
                # Use the built-in confirm_transaction call with last_valid_block_height
                result = await client.confirm_transaction(
                    tx_sig=txid,
                    commitment=Confirmed,
                    sleep_seconds=poll_interval,
                    last_valid_block_height=last_valid,
                )
            except Exception as e:
                print(f"[DEBUG] confirm_transaction RPC error: {e}")
                # wait and retry
                await sleep(poll_interval)
                continue

            # inspect the RPC response
            value = result.get("value")
            if value is None:
                print("[DEBUG] No confirmation yet—retrying...")
                await sleep(poll_interval)
                continue

            if value.get("err") is not None:
                raise Exception(f"[ERROR] Transaction execution failed on-chain: {value['err']}")

            # got a successful confirmation
            print(f"[TXN] Confirmed: {txid}")
            return txid

        # if we exit the loop, we timed out
        raise Exception(f"[ERROR] confirm_transaction timed out after {timeout}s")
        
    except Exception as final_error:
        print(f"[FATAL ERROR] Send Transaction Failed: {final_error}")
        return None
# ──────────────────────────────────────────────────────────────────────────────
# High-level helper to buy a token with real Jupiter swap
# ──────────────────────────────────────────────────────────────────────────────
async def buy_token_real(private_key: str, mint: str, sol_amount: float):
    print(f"[BUY] Buying {mint} for {sol_amount} SOL")
    kp = get_keypair_from_base58(private_key)
    lamports = int(sol_amount * 1e9)
    

    quote_response = await get_swap_route(SOL_MINT, mint, lamports)
    
    raw_tx_bytes = await get_swap_transaction(quote_response, kp.pubkey())

    try:
        txid = await send_transaction(raw_tx_bytes, kp)
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
    
    quote_response = await get_swap_route(mint, SOL_MINT, sell_amount)
    
    raw_tx_bytes = await get_swap_transaction(quote_response, kp.pubkey())

    try:
        txid = await send_transaction(raw_tx_bytes, kp)
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