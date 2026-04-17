"""Notion integration for reading/writing database pages."""
from __future__ import annotations
import httpx
from xmclaw.utils.log import logger
from .base import Integration


class NotionIntegration(Integration):
    """Notion integration using the official REST API."""

    name = "notion"
    _BASE = "https://api.notion.com/v1"
    _VERSION = "2022-06-28"

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key: str = config.get("api_key", "")
        self.database_id: str = config.get("database_id", "")
        self._client: httpx.AsyncClient | None = None

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": self._VERSION,
            "Content-Type": "application/json",
        }

    async def connect(self) -> None:
        if not self.api_key:
            logger.error("notion_api_key_missing")
            return
        self._client = httpx.AsyncClient(headers=self._headers, timeout=15)
        self._running = True
        logger.info("notion_connected")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
        self._running = False
        logger.info("notion_disconnected")

    async def send(self, text: str, target: str | None = None) -> None:
        """Create a new page in the configured database."""
        db_id = target or self.database_id
        if not self._client or not db_id:
            logger.warning("notion_no_database_id")
            return
        try:
            await self._client.post(
                f"{self._BASE}/pages",
                json={
                    "parent": {"database_id": db_id},
                    "properties": {
                        "Name": {"title": [{"text": {"content": text[:200]}}]},
                    },
                    "children": [
                        {
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {
                                "rich_text": [{"type": "text", "text": {"content": text[:2000]}}]
                            },
                        }
                    ],
                },
            )
        except Exception as e:
            logger.error("notion_send_failed", error=str(e))

    async def query_database(self, filter_: dict | None = None, sorts: list | None = None) -> list[dict]:
        """Query the configured Notion database and return page summaries."""
        if not self._client or not self.database_id:
            return []
        body: dict = {}
        if filter_:
            body["filter"] = filter_
        if sorts:
            body["sorts"] = sorts
        try:
            resp = await self._client.post(
                f"{self._BASE}/databases/{self.database_id}/query",
                json=body,
            )
            data = resp.json()
            results = []
            for page in data.get("results", []):
                props = page.get("properties", {})
                title = ""
                for v in props.values():
                    if v.get("type") == "title":
                        title_arr = v.get("title", [])
                        if title_arr:
                            title = title_arr[0].get("plain_text", "")
                        break
                results.append({
                    "id": page["id"],
                    "title": title,
                    "url": page.get("url", ""),
                    "created_time": page.get("created_time", ""),
                    "last_edited_time": page.get("last_edited_time", ""),
                })
            return results
        except Exception as e:
            logger.error("notion_query_failed", error=str(e))
            return []

    async def append_block(self, page_id: str, text: str) -> None:
        """Append a paragraph block to an existing Notion page."""
        if not self._client:
            return
        try:
            await self._client.patch(
                f"{self._BASE}/blocks/{page_id}/children",
                json={
                    "children": [
                        {
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {
                                "rich_text": [{"type": "text", "text": {"content": text[:2000]}}]
                            },
                        }
                    ]
                },
            )
        except Exception as e:
            logger.error("notion_append_failed", error=str(e))
