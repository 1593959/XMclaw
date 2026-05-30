"""OpenAI-compatible ``/v1/chat/completions`` endpoint — REMEDIATION_PLAN P2-1.

Lets clients written against the OpenAI Python SDK (Continue.dev,
Cursor, Cline, Aider, llm CLI, any code that does
``openai.OpenAI(base_url="http://127.0.0.1:8766/v1")``) talk to XMclaw
without changes. The agent loop, memory, tools, persona — all of it
— runs as normal; the client just sees a familiar JSON-RPC contract.

Scope of this MVP
=================

* ``POST /v1/chat/completions`` — non-streaming. Single AgentLoop
  turn per call. The full message history from the request body gets
  loaded into the session BEFORE the last user message is run, so
  multi-turn clients (which always re-send the whole history) get
  consistent context.
* ``GET /v1/models`` — lists configured ``llm.profiles[]`` IDs so
  clients can populate a model dropdown. Falls back to a single
  ``"default"`` entry when no profiles are configured.

Deliberately out of scope (future ADR / follow-up):

* **Streaming** (``stream=true``). XMclaw publishes streaming chunks
  on the bus; building SSE encoding + the tool-call delta protocol
  is a separate piece of work ~600 LOC. Clients that send
  ``stream=true`` currently get a single non-streamed response (with
  ``finish_reason="stop"``) — fully spec-conformant if minimally
  useful for streaming UIs.
* **OpenAI tools / function_call**. XMclaw's tools live server-side
  and are surfaced through its own AgentLoop. Surfacing them as
  OpenAI ``tools[]`` array (and handling client-side ``tool_choice``
  semantics) is its own protocol-mapping work.
* **Logprobs**. Not exposed by XMclaw's LLM providers.

Auth
====

The router prefix ``/v1`` lives OUTSIDE ``/api/v2/*`` so the
existing :class:`PairingAuthMiddleware` lets it through. Production
deployments behind a tunnel should add token auth at the reverse
proxy (Caddy / Nginx / Cloudflare Access).
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


router = APIRouter(prefix="/v1", tags=["openai-compat"])


# ─── Wire-protocol models (OpenAI shape) ──────────────────────────


class _Message(BaseModel):
    """OpenAI chat message. Only ``role`` and ``content`` are
    required for the MVP; ``name`` / ``tool_call_id`` accepted but
    ignored so clients that emit them don't get a 422."""

    role: Literal["system", "user", "assistant", "tool", "function"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None


class _ChatCompletionRequest(BaseModel):
    model: str
    messages: list[_Message]
    # Accept but ignore — present for client-compat:
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    n: int | None = None
    stop: Any | None = None
    user: str | None = None
    # Optional XMclaw extension — caller can stick a session id in
    # here to pin a continuing conversation. Falls back to header
    # ``X-Session-Id`` then auto-generated.
    session_id: str | None = None


class _Choice(BaseModel):
    index: int
    message: dict[str, Any]
    finish_reason: str = "stop"
    logprobs: None = None


class _Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class _ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[_Choice]
    usage: _Usage = Field(default_factory=_Usage)


class _ModelEntry(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "xmclaw"


class _ModelsResponse(BaseModel):
    object: str = "list"
    data: list[_ModelEntry]


# ─── Helpers ──────────────────────────────────────────────────────


def _content_to_text(content: Any) -> str:
    """OpenAI v1.1 lets ``content`` be either a plain string or a
    list of typed parts (text + image_url). Flatten to a single
    string for the AgentLoop — image_url parts surface as ``[image:
    URL]`` markers so the agent at least knows an image was sent
    (full multimodal routing is a follow-up)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type")
            if t == "text":
                out.append(str(part.get("text") or ""))
            elif t == "image_url":
                url = (part.get("image_url") or {}).get("url") or ""
                out.append(f"[image: {url}]")
        return "\n".join(p for p in out if p)
    return str(content)


def _split_messages(
    messages: list[_Message],
) -> tuple[str | None, list[dict[str, Any]], _Message]:
    """Pull the leading system prompt (if any) and the trailing user
    message out of the conversation array; return them plus the
    history-between (everything except the last user message + the
    leading system prompt) as a list of ``{role, content}`` dicts
    suitable for prepopulating the session store.

    Raises HTTPException(400) when the array doesn't end with a user
    message — the OpenAI contract is "send your full history and I
    answer the last user question."
    """
    if not messages:
        raise HTTPException(400, "messages array is empty")
    system_text: str | None = None
    body = messages[:]
    if body and body[0].role == "system":
        system_text = _content_to_text(body[0].content)
        body = body[1:]
    # Find the LAST user message; trailing assistant / tool entries
    # are tolerated (some clients add an empty assistant prefix).
    last_user_idx = -1
    for i in range(len(body) - 1, -1, -1):
        if body[i].role == "user":
            last_user_idx = i
            break
    if last_user_idx == -1:
        raise HTTPException(
            400,
            "messages array must contain at least one 'user' message",
        )
    final_user = body[last_user_idx]
    history_msgs = body[:last_user_idx]
    history = [
        {"role": m.role, "content": _content_to_text(m.content)}
        for m in history_msgs
    ]
    return system_text, history, final_user


# ─── Endpoints ────────────────────────────────────────────────────


@router.post("/chat/completions", response_model=None)
async def chat_completions(req: _ChatCompletionRequest, request: Request) -> Any:
    """Run one AgentLoop turn and return an OpenAI-shaped response."""
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(503, "agent not ready")

    _system, history, final_user = _split_messages(req.messages)

    # Session id resolution: body → header → fresh.
    session_id = (
        req.session_id
        or request.headers.get("x-session-id")
        or f"openai-{uuid.uuid4().hex[:12]}"
    )

    # Pre-load history. The AgentLoop's ``_histories`` dict is the
    # canonical in-memory store; the session_store mirrors it to
    # SQLite. We populate the in-memory side so this turn sees the
    # client-supplied conversation context, but we DO NOT write to
    # the session_store (let the agent loop's normal post-turn save
    # handle persistence — clients that re-send the whole history
    # already own context).
    if history:
        try:
            from xmclaw.core.ir import Message
            agent._histories[session_id] = [
                Message(role=h["role"], content=h["content"])
                for h in history
            ]
        except Exception:  # noqa: BLE001 — best-effort, don't block the turn
            pass

    user_text = _content_to_text(final_user.content)
    if not user_text.strip():
        raise HTTPException(
            400, "final user message has empty content",
        )

    # Pick an LLM profile if the client requested one. Falls through
    # to the registry's default when ``req.model`` doesn't match any
    # configured profile id (no error — fuzzy mapping fits the
    # OpenAI spec better than a hard 404).
    profile_id: str | None = None
    registry = getattr(request.app.state, "llm_registry", None)
    if registry is not None:
        try:
            if req.model in registry:
                profile_id = req.model
        except Exception:  # noqa: BLE001
            pass

    # Run the turn.
    result = await agent.run_turn(
        session_id=session_id,
        user_message=user_text,
        llm_profile_id=profile_id,
        channel_name="openai_compat",
    )

    finish_reason = "stop"
    if not result.ok:
        # Surface as a stop with error content rather than a 500 —
        # OpenAI SDKs handle ``finish_reason="content_filter"`` /
        # ``"length"`` gracefully; ``"stop"`` + error text in the
        # message body is the least-surprising fallback.
        finish_reason = "stop"

    response_text = result.text or (result.error or "")
    return _ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex}",
        created=int(time.time()),
        model=req.model,
        choices=[_Choice(
            index=0,
            message={"role": "assistant", "content": response_text},
            finish_reason=finish_reason,
        )],
    )


@router.get("/models", response_model=None)
async def list_models(request: Request) -> Any:
    """List configured LLM profiles as OpenAI ``model`` entries.

    Clients use this to populate model-picker dropdowns. Empty
    registry falls back to a single ``"default"`` entry so the
    OpenAI SDK's ``client.models.list()`` always returns at least
    one row.
    """
    registry = getattr(request.app.state, "llm_registry", None)
    entries: list[_ModelEntry] = []
    if registry is not None:
        try:
            for pid in registry.ids():
                entries.append(_ModelEntry(id=pid))
        except Exception:  # noqa: BLE001
            pass
    if not entries:
        entries.append(_ModelEntry(id="default"))
    return _ModelsResponse(data=entries)


__all__ = ["router"]
