"""B-93: LLM-based relevance picker — given a user query and a list
of memory files (filename + description), ask an LLM which top-K are
worth reading. Mirrors free-code's ``findRelevantMemories``.

Why an LLM and not just vector search?

* Vector search is great for **substring-style semantic** matches
  inside a known body of text. But our memory files have descriptive
  headers — we want a model that understands *intent*, not just
  surface similarity. "How do I configure Ollama?" should pull
  `embedding-setup.md` even if its body never says the word
  "configure".
* Picking is cheap on a small frontier model (the same daemon LLM the
  agent already pays for), and the cost is one extra request per
  turn — bounded.
* Vector + LLM-pick are complementary. Vector retrieves chunks
  (paragraph-grain); picker retrieves whole files (concept-grain).
  AgentLoop wires them in parallel.

Output is a list of selected ``MemoryFileEntry`` rows. The picker
NEVER fabricates a filename: it only returns files that were in the
input manifest. A malformed LLM response yields an empty list
(degrade quietly, fall back to vector-only path).
"""
from __future__ import annotations

import json
import re
from typing import Any

from xmclaw.providers.memory.file_index import MemoryFileEntry, render_manifest

_PICK_SYSTEM_PROMPT = """\
You select memory files relevant to a user's query.

You will see:
  • the user's query
  • a list of available memory files (filename: short description)

Return up to K filenames that are clearly worth reading to answer the
query. Be selective — picking irrelevant files wastes the agent's
context. If nothing is clearly relevant, return an empty list.

Output strict JSON ONLY, no prose, no code fences:

  {"files": ["filename1", "filename2"]}

Rules:
  - Only return filenames that appeared in the manifest verbatim.
  - Do not include the .md extension if the manifest didn't show it.
  - If the user is doing routine work that doesn't need recalled
    memory (e.g. "list files", "print hello"), return [].
"""


async def find_relevant_memories(
    *,
    query: str,
    entries: list[MemoryFileEntry],
    llm: Any,
    k: int = 5,
) -> list[MemoryFileEntry]:
    """Ask ``llm`` which up-to-K entries are worth reading for ``query``.

    ``llm`` must be an :class:`xmclaw.providers.llm.base.LLMProvider`
    or anything with a ``complete()`` method that takes a list of
    ``Message`` and returns an ``LLMResponse``.

    Returns a subset of ``entries`` (preserving the picker's chosen
    order). Empty list on:
      - empty entries (nothing to pick from)
      - empty / whitespace query
      - LLM call failure
      - malformed JSON response
      - LLM picked filenames not in the manifest
    """
    if not entries:
        return []
    q = (query or "").strip()
    if not q:
        return []
    manifest = render_manifest(entries)
    user_msg = (
        f"User query:\n{q}\n\n"
        f"Available memory files (up to top {k}):\n{manifest}"
    )
    # Lazy import — avoid circular core ⇄ providers ⇄ core via Message.
    from xmclaw.providers.llm.base import Message

    messages = [
        Message(role="system", content=_PICK_SYSTEM_PROMPT),
        Message(role="user", content=user_msg),
    ]
    try:
        response = await llm.complete(messages, tools=None)
    except Exception:  # noqa: BLE001
        return []
    raw = (getattr(response, "content", None) or "").strip()
    picked = _parse_pick_response(raw)
    if not picked:
        return []

    by_name = {e.name: e for e in entries}
    out: list[MemoryFileEntry] = []
    seen: set[str] = set()
    for name in picked:
        # Tolerate ``foo.md`` form too — strip the extension.
        candidate = name.strip()
        if candidate.endswith(".md"):
            candidate = candidate[:-3]
        if not candidate or candidate in seen:
            continue
        entry = by_name.get(candidate)
        if entry is None:
            # Picker hallucinated — skip silently rather than 500.
            continue
        out.append(entry)
        seen.add(candidate)
        if len(out) >= k:
            break
    return out


def _parse_pick_response(raw: str) -> list[str]:
    """Extract a list of filenames from the picker's response.

    Strict path: the response IS a JSON object with a ``files`` array.
    Lenient fallback: extract the first ``[...]`` array from anywhere
    in the response (handles models that wrap with prose despite the
    instruction). Returns ``[]`` on total failure.
    """
    if not raw:
        return []
    # Strict path
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            files = obj.get("files")
            if isinstance(files, list):
                return [str(x) for x in files if isinstance(x, (str, int))]
        if isinstance(obj, list):
            return [str(x) for x in obj if isinstance(x, (str, int))]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    # Lenient: hunt for ["a","b"] anywhere in the text
    m = re.search(r"\[[^\[\]]*\]", raw)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                return [str(x) for x in arr if isinstance(x, (str, int))]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return []


__all__ = ["find_relevant_memories"]
