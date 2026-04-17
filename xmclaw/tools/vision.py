"""Vision tool — image understanding via Claude or GPT-4o."""
from __future__ import annotations
import base64
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.log import logger

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class VisionTool(Tool):
    """Analyze or describe an image using a multimodal AI model."""

    name = "vision"
    description = (
        "Analyze an image using a multimodal AI model. "
        "Accepts a local file path, HTTPS URL, or base64 data URI. "
        "Returns a text description or answers a specific question about the image."
    )
    parameters = {
        "image": {
            "type": "string",
            "description": (
                "Image source: local file path, HTTPS URL, "
                "or data URI (data:image/...;base64,...)"
            ),
        },
        "prompt": {
            "type": "string",
            "description": (
                "Question or instruction, e.g. 'Describe this image' or 'What text is visible?'"
            ),
        },
    }

    async def execute(
        self,
        image: str,
        prompt: str = "Please describe this image in detail.",
    ) -> str:
        image_b64, media_type = await self._load_image(image)
        if not image_b64:
            return "[Vision Error: Could not load image — check path/URL and format]"

        from xmclaw.daemon.config import DaemonConfig
        cfg = DaemonConfig.load()
        llm_cfg = cfg.llm or {}
        provider = llm_cfg.get("default_provider", "anthropic")

        result = await self._call(provider, image_b64, media_type, prompt, llm_cfg)
        if result:
            return result
        # Fallback to the other provider
        alt = "openai" if provider == "anthropic" else "anthropic"
        result = await self._call(alt, image_b64, media_type, prompt, llm_cfg)
        return result or "[Vision Error: No vision-capable LLM configured]"

    async def _call(
        self, provider: str, b64: str, media_type: str, prompt: str, llm_cfg: dict
    ) -> str | None:
        if provider == "anthropic":
            return await self._call_anthropic(b64, media_type, prompt, llm_cfg.get("anthropic", {}))
        return await self._call_openai(b64, media_type, prompt, llm_cfg.get("openai", {}))

    # ── Image loading ────────────────────────────────────────────────────────

    async def _load_image(self, image: str) -> tuple[str, str]:
        """Returns (base64_data, media_type)."""
        if image.startswith("data:image"):
            try:
                header, b64 = image.split(",", 1)
                mt = header.split(":")[1].split(";")[0]
                return b64, mt
            except Exception:
                return "", "image/jpeg"

        if image.startswith("http://") or image.startswith("https://"):
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(image)
                    resp.raise_for_status()
                    ct = resp.headers.get("content-type", "image/jpeg").split(";")[0]
                    b64 = base64.b64encode(resp.content).decode()
                    return b64, ct
            except Exception as e:
                logger.error("vision_url_fetch_failed", error=str(e))
                return "", ""

        path = Path(image)
        if path.exists() and path.suffix.lower() in SUPPORTED_EXTS:
            try:
                ext = path.suffix.lower().lstrip(".")
                mt = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
                b64 = base64.b64encode(path.read_bytes()).decode()
                return b64, mt
            except Exception as e:
                logger.error("vision_file_read_failed", error=str(e))
        return "", ""

    # ── Provider calls ────────────────────────────────────────────────────────

    async def _call_anthropic(
        self, b64: str, media_type: str, prompt: str, cfg: dict
    ) -> str | None:
        api_key = cfg.get("api_key", "")
        model = cfg.get("default_model", "") or "claude-opus-4-5"
        if not api_key:
            return None
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(
                api_key=api_key,
                base_url=cfg.get("base_url", "https://api.anthropic.com"),
            )
            resp = await client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            for block in resp.content:
                if hasattr(block, "text"):
                    return block.text
        except Exception as e:
            logger.warning("vision_anthropic_failed", error=str(e))
        return None

    async def _call_openai(
        self, b64: str, media_type: str, prompt: str, cfg: dict
    ) -> str | None:
        api_key = cfg.get("api_key", "")
        model = cfg.get("default_model", "") or "gpt-4o"
        if not api_key:
            return None
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=cfg.get("base_url", "https://api.openai.com/v1"),
            )
            resp = await client.chat.completions.create(
                model=model,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            return resp.choices[0].message.content or None
        except Exception as e:
            logger.warning("vision_openai_failed", error=str(e))
        return None
