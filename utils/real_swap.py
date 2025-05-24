# utils/real_swap.py
# ──────────────────────────────────────────────────────────────────────────────
# Monkey-patch httpx.AsyncClient to swallow `proxy` kwarg so solana-py works
import httpx
_original_async_init = httpx.AsyncClient.__init__

def _patched_async_init(self, *args, proxy=None, **kwargs):
    # Ignore proxy, pass everything else through
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

RPC_URL = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API  = "https://quote-api.jup.ag/v6/swap"

# Initialize once
client = AsyncClient(RPC_URL)
client._provider = AsyncHTTPProvider(RPC_URL, timeout=30)

def get_keypair_from_base58(private_key: str) -> Keypair:
    kp = Keypair.from_bytes(base58.b58decode(private_key))
    print(f"[DEBUG] Loaded Keypair, pubkey={kp.pubkey()}")
    return kp

async def get_swap_route(input_mint: str, output_mint: str, amount: int, slippage: float = 1.0) -> dict:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        "slippage": slippage,
        "onlyDirectRoutes": False
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(JUPITER_QUOTE_API, params=params) as resp:
            json_data = await resp.json()
    print(f"[DEBUG] Quote response: {json_data}")
    routes = json_data.get("data")
    if not routes:
        raise Exception(f"No route returned from Jupiter: {json_data}")
    route = routes[0]
    print(f"[DEBUG] Selected routePlan ({len(route.get('routePlan',[]))} steps)")
    return route

async def get_swap_transaction(route: dict, user_pubkey: Pubkey) -> bytes:
    # ✅ Debug: Ensure route is serializable (Jupiter sometimes returns unserializable objects)
    try:
        json.dumps(route)
    except TypeError as e:
        print(f"[ERROR] Route not serializable: {e}")
        print(f"[DEBUG] Offending route: {route}")
        raise
    payload = {
        "route": route,
        "userPublicKey": str(user_pubkey),
        "wrapUnwrapSOL": True,
        "computeUnitPriceMicroLamports": 1,
    }

    # Omit feeAccount if not used
    # payload["feeAccount"] = "YOUR_FEE_ACCOUNT_HERE"  # if needed

    print(f"[DEBUG] Swap payload: {{'userPublicKey': {user_pubkey}, 'inAmount': {route.get('inAmount')}}}")
    async with aiohttp.ClientSession() as session:
        async with session.post(JUPITER_SWAP_API, json=payload) as resp:
            json_data = await resp.json()

    print(f"[DEBUG] Swap response: {json_data}")
    tx_b58 = json_data.get("swapTransaction")
    if not tx_b58:
        raise Exception(f"Jupiter swap failed, no transaction returned: {json_data}")
    return base58.b58decode(tx_b58)

async def send_transaction(raw_tx_bytes: bytes, keypair: Keypair) -> str:
    # Deserialize and sign
    tx = VersionedTransaction.deserialize(raw_tx_bytes)
    tx.sign([keypair])
    signed = tx.serialize()
    print(f"[DEBUG] Signed transaction size: {len(signed)} bytes")
    # Send
    sig_resp = await client.send_raw_transaction(
        signed,
        opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
    )
    sig = sig_resp.value
    print(f"[TXN] Sent: {sig}")
    await client.confirm_transaction(sig, commitment=Confirmed)
    print(f"[TXN] Confirmed: {sig}")
    return sig

async def buy_token_real(private_key: str, mint: str, sol_amount: float):
    print(f"[BUY] Attempting to buy {mint} for {sol_amount} SOL")
    kp = get_keypair_from_base58(private_key)
    lamports = int(sol_amount * 1e9)
    route = await get_swap_route(SOL_MINT, mint, lamports)
    tx_bytes = await get_swap_transaction(route, kp.pubkey())
    sig = await send_transaction(tx_bytes, kp)
    print(f"[BUY] Completed buy of {mint}, signature: {sig}")

async def sell_token_real(private_key: str, mint: str):
    from spl.token.instructions import get_associated_token_address

    print(f"[SELL] Attempting to sell all of {mint}")
    kp = get_keypair_from_base58(private_key)
    ata = get_associated_token_address(kp.pubkey(), Pubkey.from_string(mint))
    balance = await get_token_balance(ata)
    print(f"[SELL] Token account {ata}, balance = {balance}")
    if balance == 0:
        print("[SELL] Nothing to sell.")
        return
    route = await get_swap_route(mint, SOL_MINT, balance)
    tx_bytes = await get_swap_transaction(route, kp.pubkey())
    sig = await send_transaction(tx_bytes, kp)
    print(f"[SELL] Completed sell of {mint}, signature: {sig}")

async def get_token_balance(token_account: Pubkey) -> int:
    resp = await client.get_token_account_balance(token_account)
    amt = int(resp.value.amount)
    print(f"[DEBUG] Token balance for {token_account}: {amt}")
    return amt
