# utils/real_swap.py

import base58
import aiohttp
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

RPC_URL = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"

client = AsyncClient(RPC_URL)

def get_keypair_from_base58(private_key: str) -> Keypair:
    decoded = base58.b58decode(private_key)
    return Keypair.from_bytes(decoded)

async def get_swap_route(input_mint, output_mint, amount, slippage=1.0):
    url = (
        f"{JUPITER_QUOTE_API}?inputMint={input_mint}&outputMint={output_mint}"
        f"&amount={amount}&slippage={slippage}&onlyDirectRoutes=false"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            data = await response.json()
            return data["data"][0]

async def get_swap_transaction(route, user_pubkey):
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
            return base58.b58decode(data["swapTransaction"])

async def send_transaction(raw_tx_bytes: bytes, keypair: Keypair):
    tx = VersionedTransaction.deserialize(raw_tx_bytes)
    tx.sign([keypair])
    serialized = tx.serialize()
    sig = await client.send_raw_transaction(serialized, opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed))
    print(f"🔁 Sent txn: {sig}")
    await client.confirm_transaction(sig.value, commitment=Confirmed)

async def buy_token_real(private_key: str, mint: str, sol_amount: float):
    kp = get_keypair_from_base58(private_key)
    lamports = int(sol_amount * 1e9)
    route = await get_swap_route(SOL_MINT, mint, lamports)
    tx_bytes = await get_swap_transaction(route, kp.pubkey())
    await send_transaction(tx_bytes, kp)

async def sell_token_real(private_key: str, mint: str):
    kp = get_keypair_from_base58(private_key)
    token_account = get_associated_token_account(kp.pubkey(), mint)
    balance = await get_token_balance(token_account)
    if balance == 0:
        print("Nothing to sell.")
        return
    route = await get_swap_route(mint, SOL_MINT, balance)
    tx_bytes = await get_swap_transaction(route, kp.pubkey())
    await send_transaction(tx_bytes, kp)

def get_associated_token_account(owner: Pubkey, mint: str) -> Pubkey:
    from spl.token.instructions import get_associated_token_address
    return get_associated_token_address(owner, Pubkey.from_string(mint))

async def get_token_balance(token_account: Pubkey) -> int:
    resp = await client.get_token_account_balance(token_account)
    return int(resp.value.amount)
