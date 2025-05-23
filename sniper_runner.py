# sniper_runner.py

import threading, time, requests
from utils.token_checks import (
    is_token_rug, check_insider_distribution,
    check_liquidity, check_freeze_authority, check_holder_diversity
)
from utils.real_swap import buy_token_real, sell_token_real

active_threads = {}
seen_tokens = set()

def fetch_new_pumpfun_tokens():
    url = "https://frontend-api-v3.pump.fun/coins/latest"
    try:
        resp = requests.get(url)
        data = resp.json()

        # Handle single token response
        if isinstance(data, dict) and "mint" in data:
            print(f"[DEBUG] Fetched 1 token from Pump.fun")
            return [data]  # wrap in list for consistency
        print("[DEBUG] Fetched 0 tokens from Pump.fun (unexpected format)")
        return []
    except Exception as e:
        print(f"[ERROR] Failed to fetch tokens: {e}")
        return []

def start_sniping_for_user(uid, session):
    session.sniping = True
    print(f"[{uid}] ▶️ Started sniping session")

    def run():
        while session.sniping:
            new_tokens = fetch_new_pumpfun_tokens()
            for token in new_tokens:
                mint = token.get("mint")
                name = token.get("name", "Unnamed")

                if mint in seen_tokens:
                    print(f"[{uid}] 🔁 Skipping already seen token: {mint}")
                    continue
                seen_tokens.add(mint)

                # === Safety checks with logging ===
                if is_token_rug(mint):
                    print(f"[{uid}] ❌ Skipped {name} ({mint}) - Honeypot/Rug risk")
                    continue
                if not check_insider_distribution(mint):
                    print(f"[{uid}] ❌ Skipped {name} ({mint}) - Insider distribution")
                    continue
                if not check_freeze_authority(mint):
                    print(f"[{uid}] ❌ Skipped {name} ({mint}) - Freeze authority")
                    continue
                if not check_liquidity(mint, min_sol=0.5):
                    print(f"[{uid}] ❌ Skipped {name} ({mint}) - Low liquidity")
                    continue
                if not check_holder_diversity(mint):
                    print(f"[{uid}] ❌ Skipped {name} ({mint}) - Poor holder diversity")
                    continue

                print(f"[{uid}] ✅ PASSED: {name} ({mint}) - Sniping now...")

                # === Real Buy ===
                try:
                    buy_token_real(session.private_key, mint, session.sol_amount)
                    print(f"[{uid}] ✅ Bought {mint} with {session.sol_amount} SOL")
                except Exception as e:
                    print(f"[{uid}] ❌ Buy failed for {mint}: {e}")
                    continue

                # === Auto-sell after 60 seconds ===
                def auto_sell():
                    print(f"[{uid}] ⏳ Waiting 60s to auto-sell {mint}...")
                    time.sleep(60)
                    try:
                        sell_token_real(session.private_key, mint)
                        print(f"[{uid}] 🔁 Auto-sold {mint}")
                    except Exception as e:
                        print(f"[{uid}] ❌ Auto-sell failed for {mint}: {e}")

                threading.Thread(target=auto_sell, daemon=True).start()

            time.sleep(1)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    active_threads[uid] = t

def stop_sniping_for_user(uid):
    thread = active_threads.get(uid)
    if thread:
        print(f"[{uid}] 🛑 Stopping sniping session")
        del active_threads[uid]
