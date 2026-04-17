"""Static file serving for Web UI."""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from xmclaw.utils.paths import BASE_DIR

WEB_DIR = BASE_DIR / "web"


def mount_static_files(app: FastAPI) -> None:
    """Mount web UI static files."""
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    # Media uploads directory (created on demand)
    media_dir = WEB_DIR / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")

    @app.get("/")
    async def serve_index():
        return FileResponse(str(WEB_DIR / "index.html"))
