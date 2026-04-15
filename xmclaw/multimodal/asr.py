"""ASR (Automatic Speech Recognition) stub.

MVP: Whisper API integration placeholder.
Future: local Whisper.cpp, Azure Speech, etc.
"""
from pathlib import Path


class ASRClient:
    """Convert audio to text."""

    def __init__(self, provider: str = "openai"):
        self.provider = provider

    async def transcribe(self, audio_path: Path | str) -> str:
        """Transcribe audio file to text."""
        # TODO: implement actual transcription
        return "[ASR not yet implemented]"
