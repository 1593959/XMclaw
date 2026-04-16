"""System tray icon for XMclaw desktop mode."""
import sys
import threading
import webbrowser

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PYSTRAY = True
except ImportError:
    _HAS_PYSTRAY = False


DEFAULT_URL = "http://127.0.0.1:8765"


def _create_icon_image(size: int = 64) -> "Image.Image":
    """Generate a simple tray icon using Pillow (no external icon file needed)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background circle with gradient-like effect
    draw.ellipse([2, 2, size - 2, size - 2], fill=(99, 102, 241, 255))  # indigo
    draw.ellipse([4, 4, size - 4, size - 4], fill=(79, 70, 229, 255))  # darker center

    # "X" letter in center
    try:
        font = ImageFont.truetype("arial.ttf", size // 2)
    except (OSError, IOError):
        font = ImageFont.load_default()

    text = "X"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (size - tw) // 2
    ty = (size - th) // 2 - bbox[1]
    draw.text((tx, ty), text, fill=(255, 255, 255, 255), font=font)

    return img


class TrayApp:
    """System tray application that manages the daemon lifecycle and opens the browser."""

    def __init__(self, url: str = DEFAULT_URL, daemon_process=None):
        self.url = url
        self.daemon_process = daemon_process
        self._icon = None

    def open_browser(self, *_args):
        """Open the web UI in the default browser."""
        webbrowser.open(self.url)

    def restart_daemon(self, *_args):
        """Restart the daemon subprocess."""
        if self.daemon_process:
            self.daemon_process.terminate()
            self.daemon_process.wait(timeout=5)

        # Re-import and start
        from xmclaw.desktop.app import _start_daemon_subprocess
        self.daemon_process = _start_daemon_subprocess()

    def quit_app(self, *_args):
        """Stop daemon and exit."""
        if self.daemon_process:
            self.daemon_process.terminate()
            try:
                self.daemon_process.wait(timeout=5)
            except Exception:
                self.daemon_process.kill()
        if self._icon:
            self._icon.stop()

    def run(self):
        """Run the tray icon (blocks the calling thread)."""
        if not _HAS_PYSTRAY:
            print("[XMclaw] pystray not installed. Running headless (Ctrl+C to quit).", file=sys.stderr)
            print(f"[XMclaw] Open browser: {self.url}", file=sys.stderr)
            try:
                import time
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                self.quit_app()
            return

        icon_image = _create_icon_image()

        menu = pystray.Menu(
            pystray.MenuItem("Open XMclaw", self.open_browser, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart Daemon", self.restart_daemon),
            pystray.MenuItem("Quit", self.quit_app),
        )

        self._icon = pystray.Icon(
            name="XMclaw",
            icon=icon_image,
            title="XMclaw — AI Agent OS",
            menu=menu,
        )

        self._icon.run()
