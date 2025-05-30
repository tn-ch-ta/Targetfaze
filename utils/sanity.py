import re
import base58

def normalize_mint_address(raw: str) -> str | None:
    # 1) strip any trailing “pump” (case-insensitive)
    cleaned = re.sub(r"pump$", "", raw, flags=re.IGNORECASE)

    # 2) try Base58 decode
    try:
        decoded = base58.b58decode(cleaned)
    except Exception:
        return None

    # 3) must be 32 bytes (Solana mints are 32 bytes)
    if len(decoded) != 32:
        return None

    return cleaned

def is_valid_base58_address(addr: str) -> bool:
    try:
        decoded = base58.b58decode(addr)
        return len(decoded) == 32
    except Exception:
        return False