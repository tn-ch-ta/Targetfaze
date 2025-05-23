# sniper_runner.py

import asyncio
import aiohttp
from utils.token_checks import (
    is_token_rug,
    check_insider_distribution,
    check_liquidity,
    check_freeze_authority,
    check_holder_diversity
)
from utils.real_swap import buy_token_real, sell_token_real

active_tasks: dict[int, asyncio.Task] = {}
seen_tokens: set[str] = set()

async def fetch_new_pumpfun_tokens() -> list[dict]:
    url = "https://frontend-api-v3.pump.fun/coins/latest"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                data = await resp.json()
        # Pump.fun returns a single token dict
        if isinstance(data, dict) and "mint" in data:
            print(f"[DEBUG] Fetched 1 token from Pump.fun")
            return [data]
        print("[DEBUG] Fetched 0 tokens from Pump.fun (unexpected format)")
        return []
    except Exception as e:
        print(f"[ERROR] Failed to fetch tokens: {e}")
        return []

async def _snipe_loop(uid: int, session):
    print(f"[{uid}] ▶️ Started async sniping session")
    while session.sniping:
        tokens = await fetch_new_pumpfun_tokens()
        for token in tokens:
            mint = token.get("mint")
            name = token.get("name", "Unnamed")

            if mint in seen_tokens:
                print(f"[{uid}] 🔁 Skipping already seen: {mint}")
                continue
            seen_tokens.add(mint)

            # === Safety checks ===
            if await is_token_rug(mint):
                print(f"[{uid}] ❌ Skipped {name} ({mint}) – Rug/honeypot")
                continue
            if not await check_insider_distribution(mint):
                print(f"[{uid}] ❌ Skipped {name} ({mint}) – Insider heavy")
                continue
            if await check_freeze_authority(mint):
                print(f"[{uid}] ❌ Skipped {name} ({mint}) – Freeze authority")
                continue
            if not await check_liquidity(mint, min_sol=0.5):
                print(f"[{uid}] ❌ Skipped {name} ({mint}) – Low liquidity")
                continue
            if not await check_holder_diversity(mint):
                print(f"[{uid}] ❌ Skipped {name} ({mint}) – Poor diversity")
                continue

            print(f"[{uid}] ✅ PASSED: {name} ({mint}) – Executing buy")

            # === Real Buy ===
            try:
                await buy_token_real(session.private_key, mint, session.sol_amount)
                print(f"[{uid}] ✅ Bought {mint} @ {session.sol_amount} SOL")
            except Exception as e:
                print(f"[{uid}] ❌ Buy failed {mint}: {e}")
                continue

            # Schedule auto-sell in background
            async def _auto_sell():
                print(f"[{uid}] ⏳ Waiting 60s to auto-sell {mint}...")
                await asyncio.sleep(60)
                try:
                    await sell_token_real(session.private_key, mint)
                    print(f"[{uid}] 🔁 Auto-sold {mint}")
                except Exception as e:
                    print(f"[{uid}] ❌ Auto-sell failed {mint}: {e}")

            asyncio.create_task(_auto_sell())

        await asyncio.sleep(1)

async def start_sniping_for_user(uid: int, session):
    # cancel existing if present
    await stop_sniping_for_user(uid)

    session.sniping = True
    task = asyncio.create_task(_snipe_loop(uid, session))
    active_tasks[uid] = task

async def stop_sniping_for_user(uid: int):
    task = active_tasks.get(uid)
    if task:
        print(f"[{uid}] 🛑 Cancelling sniping session")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        del active_tasks[uid]
