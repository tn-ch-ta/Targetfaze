# sniper_runner.py

import asyncio
import aiohttp
import logging
import time
from utils.token_checks import passes_all_checks
from utils.real_swap import buy_token_real

logger = logging.getLogger("sniper_runner")
active_tasks: dict[int, asyncio.Task] = {}
seen_tokens: set[str] = set()

BIRDEYE_API_KEY = "38a6df7fd63941b3aaf7e25f9e38a2e8"
BIRDEYE_URL = "https://public-api.birdeye.so/defi/v2/tokens/new_listing"


async def fetch_new_birdeye_tokens(limit: int = 5) -> list[dict]:
    """
    Fetch latest new token listings from Birdeye API.
    """
    params = {
        "limit": limit,
        "meme_platform_enabled": "true",
        "time_to": int(time.time()),  # current unix timestamp
    }
    headers = {
        "accept": "application/json",
        "x-chain": "solana",
        "X-API-KEY": BIRDEYE_API_KEY,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BIRDEYE_URL, params=params, headers=headers, timeout=5) as resp:
                if resp.status != 200:
                    logger.error(f"[ERROR] Birdeye API returned {resp.status}")
                    return []
                data = await resp.json()

        # Birdeye returns results under ["data"]["items"]
        items = data.get("data", {}).get("items", [])
        if not isinstance(items, list):
            logger.warning(f"[DEBUG] Unexpected Birdeye format: {data}")
            return []

        logger.debug(f"[DEBUG] Fetched {len(items)} tokens from Birdeye")
        return items

    except Exception as e:
        logger.error(f"[ERROR] Failed to fetch tokens from Birdeye: {e}")
        return []


async def _snipe_loop(uid: int, session):
    logger.info(f"[{uid}] ▶️ Started async sniping session")
    session.busy = False  # ✅ lock to prevent overlapping buys

    while session.sniping:
        if session.busy:
            await asyncio.sleep(0.5)
            continue

        tokens = await fetch_new_birdeye_tokens()
        for token in tokens:
            mint = token.get("address")  # Birdeye's mint field
            name = token.get("symbol") or token.get("name", "Unnamed")

            if not mint:
                continue
            if mint in seen_tokens:
                logger.debug(f"[{uid}] 🔁 Skipping already seen: {mint}")
                continue
            seen_tokens.add(mint)

            hold_duration = await passes_all_checks(mint)
            if hold_duration == 0:
                logger.info(f"[{uid}] ❌ {name} ({mint}) failed checks; skipping.")
                continue

            logger.info(f"[{uid}] ✅ {name} ({mint}) passed checks → BUYING")

            try:
                session.busy = True
                await buy_token_real(session.private_key, mint, session.sol_amount)
                logger.info(f"[{uid}] ✅ Bought {mint} @{session.sol_amount} SOL")
            except Exception as e:
                logger.error(f"[{uid}] ❌ Buy failed {mint}: {e}")
                session.busy = False
                continue

            # Release lock after buy completes
            session.busy = False

        await asyncio.sleep(1)


async def start_sniping_for_user(uid: int, session):
    await stop_sniping_for_user(uid)
    session.sniping = True
    task = asyncio.create_task(_snipe_loop(uid, session))
    active_tasks[uid] = task


async def stop_sniping_for_user(uid: int):
    task = active_tasks.get(uid)
    if task:
        logger.info(f"[{uid}] 🛑 Cancelling sniping session")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        del active_tasks[uid]