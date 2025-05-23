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
        resp = requests.get(url).json()
        return resp.get("tokens", [])
    except:
        return []

def start_sniping_for_user(uid, session):
    session.sniping = True

    def run():
        while session.sniping:
            new_tokens = fetch_new_pumpfun_tokens()
            for token in new_tokens:
                mint = token.get("mint")
                if mint in seen_tokens:
                    continue
                seen_tokens.add(mint)

                # === Safety checks ===
                if is_token_rug(mint): continue
                if not check_insider_distribution(mint): continue
                if not check_freeze_authority(mint): continue
                if not check_liquidity(mint, min_sol=0.5): continue
                if not check_holder_diversity(mint): continue

                print(f"[{uid}] ✅ Passed checks: {token.get('name')} ({mint})")

                # === Real Buy ===
                try:
                    buy_token_real(session.private_key, mint, session.sol_amount)
                except Exception as e:
                    print(f"[{uid}] ❌ Buy failed: {e}")
                    continue

                # === Auto-sell after 60s ===
                def auto_sell():
                    time.sleep(60)
                    try:
                        sell_token_real(session.private_key, mint)
                        print(f"[{uid}] 🔁 Sold {mint}")
                    except Exception as e:
                        print(f"[{uid}] ❌ Auto-sell failed: {e}")

                threading.Thread(target=auto_sell, daemon=True).start()

            time.sleep(1)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    active_threads[uid] = t

def stop_sniping_for_user(uid):
    thread = active_threads.get(uid)
    if thread:
        # Threads are daemonized, just stop loop
        del active_threads[uid]
