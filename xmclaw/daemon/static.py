"""Static file serving for Web UI."""
import re

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from xmclaw.utils.paths import BASE_DIR

WEB_DIR = BASE_DIR / "web"


def _bust_cache(html: str) -> str:
    """Append a ``?v=<mtime>`` to main_new.js and styles.css in index.html.

    Without this, browsers aggressively cache the single JS bundle —
    after a web/ update users see stale UI until they Ctrl+F5. Stamping
    the mtime (read at request time) invalidates the cache on any edit
    without needing a build step.
    """
    js = WEB_DIR / "main_new.js"
    css = WEB_DIR / "styles.css"
    js_v = int(js.stat().st_mtime) if js.exists() else 0
    css_v = int(css.stat().st_mtime) if css.exists() else 0
    html = re.sub(
        r'(<script[^>]+src="/static/main_new\.js)(")',
        rf'\1?v={js_v}\2',
        html,
        count=1,
    )
    html = re.sub(
        r'(<link[^>]+href="/static/styles\.css)(")',
        rf'\1?v={css_v}\2',
        html,
        count=1,
    )
    return html


def mount_static_files(app: FastAPI) -> None:
    """Mount web UI static files."""
    if WEB_DIR.exists():
        # Mount /static for CSS and the single main JS bundle (main_new.js).
        # A sibling /src mount used to exist for a module layout we never
        # finished migrating to — removed along with web/src/.
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    # Media uploads directory (created on demand)
    media_dir = WEB_DIR / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")

    @app.get("/")
    async def serve_index():
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(_bust_cache(html))
