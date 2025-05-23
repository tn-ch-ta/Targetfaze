# utils/token_checks.py

import requests

PUMP_TOKEN_API = "https://frontend-api-v3.pump.fun/coins/latest"

def fetch_token_data(mint: str) -> dict:
    try:
        resp = requests.get(f"{PUMP_TOKEN_API}{mint}")
        return resp.json()
    except Exception as e:
        print(f"Error fetching token data: {e}")
        return {}

def is_token_rug(mint: str) -> bool:
    data = fetch_token_data(mint)
    # A token is a rug if it's not tradable or has restricted selling
    if data.get("isHoneypot") or data.get("buyTax", 0) > 10 or data.get("sellTax", 0) > 10:
        return True
    return False

def check_insider_distribution(mint: str) -> bool:
    data = fetch_token_data(mint)
    holders = data.get("holders_info", [])
    top_holder_pct = max([h.get("percentage", 0) for h in holders[:5]], default=0)
    return top_holder_pct < 10

def check_freeze_authority(mint: str) -> bool:
    data = fetch_token_data(mint)
    return not data.get("hasFreezeAuthority", True)

def check_liquidity(mint: str, min_sol: float = 0.5) -> bool:
    data = fetch_token_data(mint)
    liquidity = float(data.get("liquidity", 0))
    return liquidity >= min_sol

def check_holder_diversity(mint: str) -> bool:
    data = fetch_token_data(mint)
    holders = data.get("holders_info", [])
    if len(holders) < 10:
        return False
    total_top_10 = sum([h.get("percentage", 0) for h in holders[:10]])
    return total_top_10 < 70
