"""Streaming output utilities."""
from typing import AsyncIterator


async def collect_stream(stream: AsyncIterator[str]) -> str:
    """Collect an async stream into a single string."""
    parts = []
    async for chunk in stream:
        parts.append(chunk)
    return "".join(parts)
