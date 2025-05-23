# utils/real_swap.py

import base58
import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

RPC_URL = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"

client = Client(RPC_URL)

def get_keypair_from_base58(private_key: str) -> Keypair:
    decoded = base58.b58decode(private_key)
    return Keypair.from_bytes(decoded)

def get_swap_route(input_mint, output_mint, amount, slippage=1.0):
    url = (
        f"{JUPITER_QUOTE_API}?inputMint={input_mint}&outputMint={output_mint}"
        f"&amount={amount}&slippage={slippage}&onlyDirectRoutes=false"
    )
    response = requests.get(url).json()
    return response["data"][0]  # Best route

def get_swap_transaction(route, user_pubkey):
    payload = {
        "route": route,
        "userPublicKey": str(user_pubkey),
        "wrapUnwrapSOL": True,
        "feeAccount": None,
        "computeUnitPriceMicroLamports": 1
    }
    resp = requests.post(JUPITER_SWAP_API, json=payload).json()
    return base58.b58decode(resp["swapTransaction"])

def send_transaction(raw_tx_bytes: bytes, keypair: Keypair):
    tx = VersionedTransaction.deserialize(raw_tx_bytes)
    tx.sign([keypair])
    sig = client.send_raw_transaction(tx.serialize(), opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed))
    print(f"🔁 Sent txn: {sig}")
    client.confirm_transaction(sig["result"], commitment=Confirmed)

def buy_token_real(private_key: str, mint: str, sol_amount: float):
    kp = get_keypair_from_base58(private_key)
    lamports = int(sol_amount * 1e9)
    route = get_swap_route(SOL_MINT, mint, lamports)
    tx_bytes = get_swap_transaction(route, kp.pubkey())
    send_transaction(tx_bytes, kp)

def sell_token_real(private_key: str, mint: str):
    kp = get_keypair_from_base58(private_key)

    # This is a basic 1:1 sell of all token balance (adjust as needed)
    token_account = get_associated_token_account(kp.pubkey(), mint)
    balance = get_token_balance(token_account)
    if balance == 0:
        print("Nothing to sell.")
        return

    route = get_swap_route(mint, SOL_MINT, balance)
    tx_bytes = get_swap_transaction(route, kp.pubkey())
    send_transaction(tx_bytes, kp)

def get_associated_token_account(owner: Pubkey, mint: str):
    from solders.assoc_token import get_associated_token_address
    return get_associated_token_address(owner, Pubkey.from_string(mint))

def get_token_balance(token_account: Pubkey) -> int:
    resp = client.get_token_account_balance(token_account)
    return int(resp["result"]["value"]["amount"])
