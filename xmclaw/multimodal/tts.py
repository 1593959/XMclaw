"""TTS (Text-to-Speech) stub.

MVP: OpenAI TTS API placeholder.
Future: Edge TTS, Coqui TTS, etc.
"""
from pathlib import Path


class TTSClient:
    """Convert text to audio."""

    def __init__(self, provider: str = "openai"):
        self.provider = provider

    async def synthesize(self, text: str, output_path: Path | str) -> str:
        """Synthesize text to audio file."""
        # TODO: implement actual synthesis
        return "[TTS not yet implemented]"
