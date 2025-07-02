# utils/real_swap.py

# ──────────────────────────────────────────────────────────────────────────────
# Monkey-patch httpx.AsyncClient to swallow the `proxy` kwarg so solana-py works
import httpx
_original_async_init = httpx.AsyncClient.__init__
def _patched_async_init(self, *args, proxy=None, **kwargs):
    return _original_async_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_async_init
# ──────────────────────────────────────────────────────────────────────────────

import base64, aiohttp, json, logging
from solana.keypair import Keypair
from solana.publickey import PublicKey
from solana.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed

logger = logging.getLogger("real_swap")
logging.basicConfig(level=logging.INFO)

RPC_URL           = "https://api.mainnet-beta.solana.com"
SOL_MINT          = "So11111111111111111111111111111111111111112"
JUP_QUOTE_API     = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP_API      = "https://lite-api.jup.ag/swap/v1/swap"

# reuse one client for on-chain calls
client = AsyncClient(RPC_URL)


async def get_swap_route(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100) -> dict:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        "slippageBps": slippage_bps,
        "onlyDirectRoutes": "false",
        "restrictIntermediateTokens": "true",
    }
    async with aiohttp.ClientSession() as sess:
        async with sess.get(JUP_QUOTE_API, params=params) as resp:
            data = await resp.json()
    if "routePlan" not in data:
        raise RuntimeError(f"Quote error: {data}")
    return data


async def get_swap_transaction(quote: dict, user_pk: str) -> bytes:
    payload = {
        "quoteResponse": quote,
        "userPublicKey": user_pk,
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
    async with aiohttp.ClientSession() as sess:
        async with sess.post(JUP_SWAP_API, json=payload) as resp:
            data = await resp.json()
    raw = data.get("swapTransaction")
    if isinstance(raw, str):
        return base64.b64decode(raw)
    if isinstance(raw, list):
        return bytes(raw)
    raise RuntimeError(f"Bad swapTransaction: {type(raw)}")


async def send_transaction(signed_tx: bytes) -> str:
    """
    Jupiter gives back a fully-signed VersionedTransaction as raw bytes.
    We just deserialize → re-serialize → send via solana-py.
    """
    # 1) Deserialize
    tx: VersionedTransaction = VersionedTransaction.deserialize(signed_tx)

    # 2) Serialize to wire format
    serialized = tx.serialize()

    # 3) Send + confirm
    res = await client.send_raw_transaction(
        serialized,
        opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
    )
    sig = res.value
    await client.confirm_transaction(sig, commitment=Confirmed)
    return sig


async def buy_token_real(priv_b58: str, mint: str, sol_amount: float):
    """
    1) Decodes your Base58 keypair (unused for signing here),
    2) Gets a quote,
    3) Fetches Jupiter’s fully-signed tx,
    4) Sends it.
    """
    kp = Keypair.from_secret_key(__import__("base58").b58decode(priv_b58))
    lam = int(sol_amount * 1e9)
    quote = await get_swap_route(SOL_MINT, mint, lam)
    raw_tx = await get_swap_transaction(quote, str(kp.public_key))
    sig = await send_transaction(raw_tx)
    logger.info(f"Bought {mint}: {sig}")


async def sell_token_real(priv_b58: str, mint: str):
    from spl.token.instructions import get_associated_token_address

    kp = Keypair.from_secret_key(__import__("base58").b58decode(priv_b58))
    ata = get_associated_token_address(kp.public_key, PublicKey(mint))

    bal = await client.get_token_account_balance(ata, commitment=Confirmed)
    amount = int(int(bal.value.amount) * 0.98)
    if amount == 0:
        logger.info("Nothing to sell.")
        return

    quote = await get_swap_route(mint, SOL_MINT, amount)
    raw_tx = await get_swap_transaction(quote, str(kp.public_key))
    sig = await send_transaction(raw_tx)
    logger.info(f"Sold {mint}: {sig}")


# ──────────────────────────────────────────────────────────────────────────────
# Helper to fetch a token account’s balance (in raw amount)
# ──────────────────────────────────────────────────────────────────────────────
async def get_token_balance(token_account: Pubkey) -> int:
    resp = await client.get_token_account_balance(token_account)
    amt  = int(resp.value.amount)
    print(f"[DEBUG] Token balance for {token_account}: {amt}")
    return amt