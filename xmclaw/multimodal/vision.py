"""Vision / image understanding stub.

MVP: GPT-4V / Claude vision API placeholder.
Future: local LLaVA, Qwen-VL, etc.
"""
from pathlib import Path


class VisionClient:
    """Analyze images."""

    def __init__(self, provider: str = "openai"):
        self.provider = provider

    async def describe(self, image_path: Path | str) -> str:
        """Generate description of an image."""
        # TODO: implement actual vision inference
        return "[Vision not yet implemented]"
