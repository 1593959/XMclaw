"""Simple HTTP client for desktop UI to fetch daemon data."""
import asyncio
import aiohttp


async def fetch_json(method: str, path: str, json_data: dict | None = None) -> dict:
    url = f"http://127.0.0.1:8765{path}"
    async with aiohttp.ClientSession() as session:
        if method == "GET":
            async with session.get(url) as resp:
                return await resp.json()
        elif method == "POST":
            async with session.post(url, json=json_data) as resp:
                return await resp.json()
        else:
            raise ValueError(f"Unsupported method: {method}")
