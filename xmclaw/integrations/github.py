"""GitHub integration for issue/PR notifications and comments."""
from __future__ import annotations
import asyncio
import httpx
from xmclaw.utils.log import logger
from .base import Integration


class GitHubIntegration(Integration):
    """GitHub integration using the REST API (no heavy SDK needed)."""

    name = "github"

    def __init__(self, config: dict):
        super().__init__(config)
        self.token: str = config.get("token", "")
        self.repo: str = config.get("repo", "")          # "owner/repo"
        self.poll_interval: int = int(config.get("poll_interval", 60))
        self._last_event_id: str = ""
        self._poll_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def connect(self) -> None:
        if not self.token or not self.repo:
            logger.error("github_config_missing")
            return
        self._client = httpx.AsyncClient(headers=self._headers, timeout=15)
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_events())
        logger.info("github_connected", repo=self.repo)

    async def disconnect(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        if self._client:
            await self._client.aclose()
        self._running = False
        logger.info("github_disconnected")

    async def _poll_events(self) -> None:
        while self._running:
            try:
                resp = await self._client.get(
                    f"https://api.github.com/repos/{self.repo}/events",
                    params={"per_page": 10},
                )
                if resp.status_code == 200:
                    events = resp.json()
                    new_events = []
                    for ev in events:
                        if ev["id"] == self._last_event_id:
                            break
                        new_events.append(ev)
                    if new_events:
                        self._last_event_id = events[0]["id"]
                    for ev in reversed(new_events):
                        etype = ev.get("type", "")
                        actor = ev.get("actor", {}).get("login", "unknown")
                        payload = ev.get("payload", {})
                        text = self._format_event(etype, actor, payload)
                        if text:
                            await self._dispatch(
                                f"github:{actor}",
                                text,
                                {"event_type": etype, "repo": self.repo, "platform": "github"},
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("github_poll_error", error=str(e))
            await asyncio.sleep(self.poll_interval)

    def _format_event(self, etype: str, actor: str, payload: dict) -> str:
        if etype == "IssuesEvent":
            action = payload.get("action", "")
            issue = payload.get("issue", {})
            return f"[GitHub] {actor} {action} issue #{issue.get('number')}: {issue.get('title', '')}"
        elif etype == "PullRequestEvent":
            action = payload.get("action", "")
            pr = payload.get("pull_request", {})
            return f"[GitHub] {actor} {action} PR #{pr.get('number')}: {pr.get('title', '')}"
        elif etype == "PushEvent":
            commits = payload.get("commits", [])
            ref = payload.get("ref", "").replace("refs/heads/", "")
            msgs = [c.get("message", "")[:60] for c in commits[:3]]
            return f"[GitHub] {actor} pushed {len(commits)} commit(s) to {ref}: {'; '.join(msgs)}"
        elif etype == "IssueCommentEvent":
            comment = payload.get("comment", {})
            issue = payload.get("issue", {})
            return f"[GitHub] {actor} commented on #{issue.get('number')}: {comment.get('body', '')[:100]}"
        return ""

    async def send(self, text: str, target: str | None = None) -> None:
        """Create a comment on an issue/PR. target = 'issues/123' or 'pulls/123'."""
        if not self._client or not self.repo:
            return
        resource = target or "issues/1"
        try:
            await self._client.post(
                f"https://api.github.com/repos/{self.repo}/{resource}/comments",
                json={"body": text},
            )
        except Exception as e:
            logger.error("github_send_failed", error=str(e))

    async def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> dict:
        """Helper: create a new GitHub issue."""
        if not self._client:
            return {}
        try:
            resp = await self._client.post(
                f"https://api.github.com/repos/{self.repo}/issues",
                json={"title": title, "body": body, "labels": labels or []},
            )
            return resp.json()
        except Exception as e:
            logger.error("github_create_issue_failed", error=str(e))
            return {}
