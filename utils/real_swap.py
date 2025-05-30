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
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import Message, MessageV0
from solders.hash import Hash
from solders.signature import Signature
from solana.rpc.async_api import AsyncClient
from solana.rpc.providers.async_http import AsyncHTTPProvider
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
# Convert a Base58‐encoded private key into a Keypair, with validation
# ──────────────────────────────────────────────────────────────────────────────
def get_keypair_from_base58(private_key: str) -> Keypair:
    # We assume the input is valid Base58‐encoded 64‐byte keypair
    try:
        raw_bytes = base58.b58decode(private_key)
    except Exception as e:
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
# ──────────────────────────────────────────────────────────────────────────────
# 1. Get swap transaction from Jupiter and decode the Base64 string
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

    tx_raw = json_data.get("swapTransaction")
    if not tx_raw:
        raise Exception(f"Jupiter swap failed, no transaction returned: {json_data}")

    # Decode swapTransaction (Base64 string or list of ints)
    if isinstance(tx_raw, str):
        print(f"[DEBUG] swapTransaction is a Base64 string (len={len(tx_raw)})")
        try:
            return base64.b64decode(tx_raw)
        except Exception as e:
            snippet = tx_raw[:10] + ("..." if len(tx_raw) > 10 else "")
            raise Exception(f"[ERROR] base64.b64decode failed on swapTransaction (“{snippet}”): {e}")

    elif isinstance(tx_raw, list):
        print(f"[DEBUG] swapTransaction is a list of ints (len={len(tx_raw)})")
        try:
            return bytes(tx_raw)
        except Exception as e:
            raise Exception(f"[ERROR] Converting swapTransaction list[int] → bytes failed: {e}")

    else:
        raise Exception(f"[ERROR] Unexpected swapTransaction format: {type(tx_raw)}")
# ──────────────────────────────────────────────────────────────────────────────
# Send a signed, versioned transaction to Solana mainnet
# ──────────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────────
# 2. Sign and send the decoded versioned transaction (raw_tx_bytes)
# ──────────────────────────────────────────────────────────────────────────────
async def send_transaction(raw_tx_bytes: bytes, keypair: Keypair) -> str:
    try:
        # 1) Deserialize the incoming Base64‐decoded tx into a solders VersionedTransaction
        tx: VersionedTransaction = VersionedTransaction.from_bytes(raw_tx_bytes)

        # 2) Grab the MessageV0 and serialize it to bytes for signing.
        #    In solders, `bytes(tx.message)` is the correct way to get the message‐bytes.
        msg_bytes = bytes(tx.message)

        # 3) Sign those message‐bytes with your Keypair -> this returns a solders Signature
        sig: Signature = keypair.sign_message(msg_bytes)

        # 4) Find YOUR signer index in the tx.message.account_keys array.
        #    VersionedTransaction.message.account_keys is a Vec<Pubkey>.
        #
        #    In a versioned tx, the first `num_required_signatures` accounts
        #    are the “signer” accounts. So we scan through them to find
        #    which index matches your keypair’s public key.
        #
        #    If you’re the only signer, this loop still works (it’ll find index 0 if
        #    your pubkey is the very first account_key).
        #
        signer_index = None
        for idx, acct in enumerate(tx.message.account_keys):
            if acct == Pubkey.from_bytes(keypair.pubkey().to_bytes()):
                signer_index = idx
                break

        if signer_index is None:
            raise Exception(
                f"[ERROR] Could not find my public key ({keypair.pubkey()}) among the transaction’s account_keys. "
                "Make sure you passed the correct raw_tx_bytes and that your Keypair is actually a required signer in that tx."
            )

        # 5) Copy the existing signatures list (if any) to a mutable Python list.
        #    solders.Transaction.signatures is a Vec<Signature>, which behaves like a tuple/list.
        orig_sigs = list(tx.signatures)

        # 6) If the original tx had fewer slots than needed, pad with “empty” signatures:
        #    In versioned txes, sig slots must exactly match num_required_signatures.
        #    But solders.from_bytes(...) should have filled them with placeholder “Signature::default()”
        #    if they were empty. We just double‐check length:
        if len(orig_sigs) < len(tx.signatures):
            # This normally shouldn’t happen—solders.from_bytes gives you the right length. But just in case:
            orig_sigs += [Signature.default()] * (len(tx.signatures) - len(orig_sigs))

        # 7) Replace only the slot at `signer_index` with your fresh signature.
        orig_sigs[signer_index] = sig

        # 8) Reconstruct a new VersionedTransaction using the SAME message but UPDATED signatures:
        signed_tx = VersionedTransaction(tx.message, orig_sigs)

        # 9) Serialize the signed transaction as bytes (again, no .serialize()):
        serialized_bytes = bytes(signed_tx)
        print(f"[DEBUG] Signed transaction size: {len(serialized_bytes)} bytes")
        print(f"[DEBUG] Signatures after replacement:")
        for i, s in enumerate(orig_sigs):
            print(f"  slot {i:>2}:  {s}")

        # 10) Send the fully‐signed raw bytes to the cluster—do NOT pass keypairs here.
        sig_resp = await client.send_raw_transaction(
            serialized_bytes,
            opts={"skip_preflight": True, "preflight_commitment": "confirmed"}
        )
        sig_str = sig_resp.value
        print(f"[TXN] Sent:      {sig_str}")

        # 11) Wait for confirmation
        await client.confirm_transaction(sig_str, commitment="confirmed")
        print(f"[TXN] Confirmed: {sig_str}")

        return sig_str

    except Exception as e:
        raise Exception(f"[ERROR] Final send_transaction (buy) failed: {e}")
# ──────────────────────────────────────────────────────────────────────────────
# High‐level helper to buy a token with real Jupiter swap
# ──────────────────────────────────────────────────────────────────────────────
async def buy_token_real(private_key: str, mint: str, sol_amount: float):
    print(f"[BUY] Buying {mint} for {sol_amount} SOL")
    kp = get_keypair_from_base58(private_key)
    lamports = int(sol_amount * 1e9)

    quote_response = await get_swap_route(SOL_MINT, mint, lamports)
    raw_tx_bytes       = await get_swap_transaction(quote_response, kp.pubkey())

    try:
        sig = await send_transaction(raw_tx_bytes, kp)
    except Exception as e:
        raise Exception(f"[ERROR] Final send_transaction (buy) failed: {e}")

    print(f"[BUY] Completed buy of {mint}, signature: {sig}")

# ──────────────────────────────────────────────────────────────────────────────
# High‐level helper to sell (98% of balance) using real Jupiter swap
# ──────────────────────────────────────────────────────────────────────────────
async def sell_token_real(private_key: str, mint: str):
    from spl.token.instructions import get_associated_token_address

    print(f"[SELL] Selling all of {mint}")
    kp = get_keypair_from_base58(private_key)

    try:
        ata = get_associated_token_address(kp.pubkey(), Pubkey.from_string(mint))
    except Exception as e:
        raise Exception(f"[ERROR] Could not derive ATA for {mint}: {e}")

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
    raw_tx_bytes       = await get_swap_transaction(quote_response, kp.pubkey())

    try:
        sig = await send_transaction(raw_tx_bytes, kp)
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