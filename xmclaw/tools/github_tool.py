"""GitHub integration tool for XMclaw."""
import json
from typing import Any

from xmclaw.tools.base import Tool
from xmclaw.utils.log import logger


class GitHubTool(Tool):
    name = "github"
    description = (
        "Interact with GitHub repositories via REST API. "
        "Actions: get_repo, list_issues, get_issue, create_issue, list_prs, get_pr, create_pr_comment."
    )

    def __init__(self):
        self.token = self._load_token()

    def _load_token(self) -> str:
        """Load GitHub token from environment or config."""
        import os
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            try:
                from xmclaw.daemon.config import DaemonConfig
                cfg = DaemonConfig.load()
                token = (cfg.tools or {}).get("github", {}).get("token", "")
            except Exception:
                pass
        return token

    def get_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "get_repo", "list_issues", "get_issue", "create_issue",
                            "list_prs", "get_pr", "create_pr_comment"
                        ],
                        "description": "GitHub action",
                    },
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "issue_number": {"type": "integer", "description": "Issue number"},
                    "pr_number": {"type": "integer", "description": "PR number"},
                    "title": {"type": "string", "description": "Issue/PR title"},
                    "body": {"type": "string", "description": "Issue/PR/comment body"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                },
                "required": ["action", "owner", "repo"],
            },
        }

    async def execute(self, action: str, owner: str, repo: str, **kwargs) -> str:
        import aiohttp

        if not self.token:
            return "[Error: GITHUB_TOKEN not configured. Set GITHUB_TOKEN env var or add github.token to daemon config.]"

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        base = f"https://api.github.com/repos/{owner}/{repo}"

        action = action.lower()
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                if action == "get_repo":
                    async with session.get(base) as resp:
                        data = await resp.json()
                        return json.dumps({
                            "name": data.get("name"),
                            "description": data.get("description"),
                            "stars": data.get("stargazers_count"),
                            "forks": data.get("forks_count"),
                            "open_issues": data.get("open_issues_count"),
                            "url": data.get("html_url"),
                        }, ensure_ascii=False, indent=2)

                elif action == "list_issues":
                    state = kwargs.get("state", "open")
                    async with session.get(f"{base}/issues?state={state}&per_page=10") as resp:
                        data = await resp.json()
                        issues = [{"number": i.get("number"), "title": i.get("title"), "state": i.get("state"), "url": i.get("html_url")} for i in data]
                        return json.dumps(issues, ensure_ascii=False, indent=2)

                elif action == "get_issue":
                    num = kwargs.get("issue_number")
                    if not num:
                        return "[Error: issue_number required]"
                    async with session.get(f"{base}/issues/{num}") as resp:
                        data = await resp.json()
                        return json.dumps({
                            "number": data.get("number"),
                            "title": data.get("title"),
                            "state": data.get("state"),
                            "body": data.get("body"),
                            "url": data.get("html_url"),
                        }, ensure_ascii=False, indent=2)

                elif action == "create_issue":
                    title = kwargs.get("title", "")
                    body = kwargs.get("body", "")
                    if not title:
                        return "[Error: title required]"
                    async with session.post(f"{base}/issues", json={"title": title, "body": body}) as resp:
                        data = await resp.json()
                        return json.dumps({
                            "number": data.get("number"),
                            "title": data.get("title"),
                            "url": data.get("html_url"),
                        }, ensure_ascii=False, indent=2)

                elif action == "list_prs":
                    state = kwargs.get("state", "open")
                    async with session.get(f"{base}/pulls?state={state}&per_page=10") as resp:
                        data = await resp.json()
                        prs = [{"number": p.get("number"), "title": p.get("title"), "state": p.get("state"), "url": p.get("html_url")} for p in data]
                        return json.dumps(prs, ensure_ascii=False, indent=2)

                elif action == "get_pr":
                    num = kwargs.get("pr_number")
                    if not num:
                        return "[Error: pr_number required]"
                    async with session.get(f"{base}/pulls/{num}") as resp:
                        data = await resp.json()
                        return json.dumps({
                            "number": data.get("number"),
                            "title": data.get("title"),
                            "state": data.get("state"),
                            "body": data.get("body"),
                            "url": data.get("html_url"),
                        }, ensure_ascii=False, indent=2)

                elif action == "create_pr_comment":
                    num = kwargs.get("pr_number")
                    body = kwargs.get("body", "")
                    if not num or not body:
                        return "[Error: pr_number and body required]"
                    async with session.post(f"{base}/issues/{num}/comments", json={"body": body}) as resp:
                        data = await resp.json()
                        return json.dumps({"id": data.get("id"), "url": data.get("html_url")}, ensure_ascii=False, indent=2)

                else:
                    return f"[Error: Unknown action '{action}']"
        except Exception as e:
            logger.error("github_tool_error", action=action, owner=owner, repo=repo, error=str(e))
            return f"[Error: {e}]"
