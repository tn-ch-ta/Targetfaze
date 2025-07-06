# sniper_runner.py

import asyncio
import aiohttp
import logging
from utils.token_checks import passes_all_checks
from utils.real_swap import buy_token_real, sell_token_real

logger = logging.getLogger("sniper_runner")
active_tasks: dict[int, asyncio.Task] = {}
seen_tokens: set[str] = set()

async def fetch_new_pumpfun_tokens() -> list[dict]:
    url = "https://frontend-api-v3.pump.fun/coins/latest"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                data = await resp.json()
        if isinstance(data, dict) and "mint" in data:
            logger.debug("[DEBUG] Fetched 1 token from Pump.fun")
            return [data]
        logger.warning("[DEBUG] Unexpected format from Pump.fun; got data: %r", data)
        return []
    except Exception as e:
        logger.error(f"[ERROR] Failed to fetch tokens from Pump.fun: {e}")
        return []

async def _snipe_loop(uid: int, session):
    logger.info(f"[{uid}] ▶️ Started async sniping session")
    session.busy = False  # ✅ NEW: initialize busy flag

    while session.sniping:
        if session.busy:
            await asyncio.sleep(0.5)
            continue

        tokens = await fetch_new_pumpfun_tokens()
        for token in tokens:
            mint = token.get("mint")
            name = token.get("name", "Unnamed")

            if mint in seen_tokens:
                logger.debug(f"[{uid}] 🔁 Skipping already seen: {mint}")
                continue
            seen_tokens.add(mint)

            hold_duration = await passes_all_checks(mint)
            if hold_duration == 0:
                logger.info(f"[{uid}] ❌ {name} ({mint}) failed safety checks; skipping.")
                continue

            logger.info(f"[{uid}] ✅ {name} ({mint}) passed checks → buy & hold {hold_duration}s")

            try:
                session.busy = True  # ✅ NEW: Lock all further buys
                await buy_token_real(session.private_key, mint, session.sol_amount)
                logger.info(f"[{uid}] ✅ Bought {mint} @{session.sol_amount} SOL")
            except Exception as e:
                logger.error(f"[{uid}] ❌ Buy failed {mint}: {e}")
                session.busy = False  # ✅ Unlock on failure
                continue

            async def _auto_sell(m: str, dur: int):
                logger.info(f"[{uid}] ⏳ Holding {m} for {dur}s before auto-sell")
                await asyncio.sleep(dur)
                try:
                    await sell_token_real(session.private_key, m)
                    logger.info(f"[{uid}] 🔁 Auto-sold 98% of {m}")
                except Exception as e:
                    logger.error(f"[{uid}] ❌ Auto-sell failed {m}: {e}")
                finally:
                    session.busy = False  # ✅ NEW: Release lock after sell

            asyncio.create_task(_auto_sell(mint, hold_duration))

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