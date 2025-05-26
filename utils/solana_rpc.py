
# Helper to send raw JSON-RPC to Solana
async def _rpc(method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with aiohttp.ClientSession() as sess:
        async with sess.post(RPC_URL, json=payload, timeout=10) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("result")