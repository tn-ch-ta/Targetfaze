# utils/real_swap.py
# ──────────────────────────────────────────────────────────────────────────────
# Monkey-patch httpx.AsyncClient to swallow `proxy` kwarg so solana-py works
# ──────────────────────────────────────────────────────────────────────────────
import httpx
_original_async_init = httpx.AsyncClient.__init__
def _patched_async_init(self, *args, proxy=None, **kwargs):
    # Ignore proxy, pass everything else through
    return _original_async_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_async_init
# ──────────────────────────────────────────────────────────────────

# utils/real_swap.py

import base58
import aiohttp
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.async_api import AsyncClient
from solana.rpc.providers.async_http import AsyncHTTPProvider
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

RPC_URL = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"

client = AsyncClient(RPC_URL)
client._provider = AsyncHTTPProvider(RPC_URL, timeout=30)

def get_keypair_from_base58(private_key: str) -> Keypair:
    return Keypair.from_bytes(base58.b58decode(private_key))

async def get_swap_route(input_mint: str, output_mint: str, amount: int, slippage: float = 1.0) -> dict:
    url = (
        f"{JUPITER_QUOTE_API}?inputMint={input_mint}&outputMint={output_mint}"
        f"&amount={amount}&slippage={slippage}&onlyDirectRoutes=false"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if "data" not in data or not data["data"]:
                raise Exception(f"No route returned: {data}")
            return data["data"][0]

async def get_swap_transaction(route: dict, user_pubkey: Pubkey) -> bytes:
    payload = {
        "route": route,
        "userPublicKey": str(user_pubkey),
        "wrapUnwrapSOL": True,
        "feeAccount": None,
        "computeUnitPriceMicroLamports": 1
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(JUPITER_SWAP_API, json=payload) as resp:
            data = await resp.json()
            if "swapTransaction" not in data:
                raise Exception(f"Jupiter swap failed: {data}")
            return base58.b58decode(data["swapTransaction"])

async def send_transaction(raw_tx_bytes: bytes, keypair: Keypair) -> Signature:
    tx = VersionedTransaction.deserialize(raw_tx_bytes)
    tx.sign([keypair])
    sig_resp = await client.send_raw_transaction(
        tx.serialize(), opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
    )
    await client.confirm_transaction(sig_resp.value, commitment=Confirmed)
    print(f"[TXN] Sent: {sig_resp.value}")
    return sig_resp.value

async def buy_token_real(private_key: str, mint: str, sol_amount: float):
    kp = get_keypair_from_base58(private_key)
    lamports = int(sol_amount * 1e9)
    print(f"[BUY] {sol_amount} SOL -> {mint[:6]}")
    route = await get_swap_route(SOL_MINT, mint, lamports)
    tx_bytes = await get_swap_transaction(route, kp.pubkey())
    await send_transaction(tx_bytes, kp)

async def sell_token_real(private_key: str, mint: str):
    from spl.token.instructions import get_associated_token_address

    kp = get_keypair_from_base58(private_key)
    ata = get_associated_token_address(kp.pubkey(), Pubkey.from_string(mint))
    balance = await get_token_balance(ata)
    if balance == 0:
        print("[SELL] Nothing to sell.")
        return
    print(f"[SELL] {mint[:6]} -> SOL ({balance})")
    route = await get_swap_route(mint, SOL_MINT, balance)
    tx_bytes = await get_swap_transaction(route, kp.pubkey())
    await send_transaction(tx_bytes, kp)

async def get_token_balance(token_account: Pubkey) -> int:
    resp = await client.get_token_account_balance(token_account)
    return int(resp.value.amount)
