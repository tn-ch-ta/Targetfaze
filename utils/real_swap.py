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
RPC_URL            = "https://api.mainnet-beta.solana.com"
SOL_MINT           = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API  = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API   = "https://lite-api.jup.ag/swap/v1/swap"

# shared AsyncClient
client = AsyncClient(RPC_URL)
client._provider = AsyncHTTPProvider(RPC_URL, timeout=30)

def get_keypair_from_base58(private_key: str) -> Keypair:
    kp = Keypair.from_bytes(base58.b58decode(private_key))
    print(f"[DEBUG] Loaded Keypair, pubkey={kp.pubkey()}")
    return kp

async def get_swap_route(input_mint: str, output_mint: str, amount: int, slippage: float = 1.0) -> dict:
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
            json_data = await resp.json()

    print(f"[DEBUG] Quote response:\n{json.dumps(json_data, indent=2)}")
    if not isinstance(json_data, dict) or "routePlan" not in json_data:
        raise Exception(f"No valid quote returned from Jupiter: {json_data}")

    # Return the full quoteResponse object
    return json_data

async def get_swap_transaction(quote_response: dict, user_pubkey: Pubkey) -> bytes:
    # log any booleans in the quote for debugging
    log_bool_fields(quote_response)

    # Clean only None values
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
                print(f"[ERROR] Swap JSON parse failed: {e}\n{text}")
                raise

    print(f"[DEBUG] Swap API response:\n{json.dumps(json_data, indent=2)}")
    tx_b58 = json_data.get("swapTransaction")
    if not tx_b58:
        raise Exception(f"Jupiter swap failed, no transaction returned: {json_data}")

    return base58.b58decode(tx_b58)

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

async def buy_token_real(private_key: str, mint: str, sol_amount: float):
    print(f"[BUY] Buying {mint} for {sol_amount} SOL")
    kp = get_keypair_from_base58(private_key)
    lamports = int(sol_amount * 1e9)

    quote_response = await get_swap_route(SOL_MINT, mint, lamports)
    tx_bytes       = await get_swap_transaction(quote_response, kp.pubkey())
    sig            = await send_transaction(tx_bytes, kp)

    print(f"[BUY] Completed buy of {mint}, signature: {sig}")

async def sell_token_real(private_key: str, mint: str):
    from spl.token.instructions import get_associated_token_address

    print(f"[SELL] Selling all of {mint}")
    kp = get_keypair_from_base58(private_key)
    ata     = get_associated_token_address(kp.pubkey(), Pubkey.from_string(mint))
    balance = await get_token_balance(ata)
    print(f"[SELL] Token account {ata}, balance = {balance}")
    if balance == 0:
        print("[SELL] Nothing to sell.")
        return
    
    sell_amount = int(balance * 0.98)
    if sell_amount == 0:
        print("[SELL] 98% of balance is 0, skipping.")
        return

    quote_response = await get_swap_route(mint, SOL_MINT, balance)
    tx_bytes       = await get_swap_transaction(quote_response, kp.pubkey())
    sig            = await send_transaction(tx_bytes, kp)

    print(f"[SELL] Completed sell of {mint}, signature: {sig}")

async def get_token_balance(token_account: Pubkey) -> int:
    resp = await client.get_token_account_balance(token_account)
    amt  = int(resp.value.amount)
    print(f"[DEBUG] Token balance for {token_account}: {amt}")
    return amt
