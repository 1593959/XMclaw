"""Computer Use tool for desktop automation."""
import base64
import io
from typing import Any

from xmclaw.tools.base import Tool


class ComputerUseTool(Tool):
    name = "computer_use"
    description = (
        "Control the computer desktop: take screenshots, move mouse, click, type text, press keys. "
        "Actions: screenshot, click, move, type, key."
    )

    def get_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["screenshot", "click", "move", "type", "key"],
                        "description": "The action to perform",
                    },
                    "x": {"type": "integer", "description": "X coordinate for click/move"},
                    "y": {"type": "integer", "description": "Y coordinate for click/move"},
                    "text": {"type": "string", "description": "Text to type"},
                    "key": {"type": "string", "description": "Key to press (e.g., enter, ctrl, alt, tab)"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                },
                "required": ["action"],
            },
        }

    async def execute(self, action: str, **kwargs) -> str:
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
        except ImportError:
            return "[Error: pyautogui is not installed. Run: pip install pyautogui]"

        action = action.lower()

        if action == "screenshot":
            return self._screenshot()
        elif action == "click":
            x = kwargs.get("x")
            y = kwargs.get("y")
            button = kwargs.get("button", "left")
            if x is None or y is None:
                return "[Error: click requires x and y coordinates]"
            pyautogui.click(int(x), int(y), button=button)
            return f"Clicked {button} at ({x}, {y})"
        elif action == "move":
            x = kwargs.get("x")
            y = kwargs.get("y")
            if x is None or y is None:
                return "[Error: move requires x and y coordinates]"
            pyautogui.moveTo(int(x), int(y))
            return f"Moved mouse to ({x}, {y})"
        elif action == "type":
            text = kwargs.get("text", "")
            pyautogui.typewrite(text, interval=0.01)
            return f"Typed: {text[:50]}"
        elif action == "key":
            key = kwargs.get("key", "")
            pyautogui.press(key)
            return f"Pressed key: {key}"
        else:
            return f"[Error: Unknown action '{action}']"

    def _screenshot(self) -> str:
        try:
            import mss
        except ImportError:
            return "[Error: mss is not installed. Run: pip install mss]"

        with mss.mss() as sct:
            monitor = sct.monitors[0]  # Full screen
            img = sct.grab(monitor)
            from PIL import Image
            pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
            buffer = io.BytesIO()
            pil_img.save(buffer, format="PNG")
            b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
            return f"[Screenshot] data:image/png;base64,{b64[:200]}... (total {len(b64)} chars)"
