"""GroupOrchestrator — 多 agent 群聊的编排循环（AutoGen GroupChat 式）.

Group G1 (2026-06-06)。给定一个 :class:`GroupRoom`，驱动「选讲者 → 跑该 agent
的 run_turn → 把回复并入房间 transcript」的循环，直到交回用户 / 达 max_rounds。

设计要点（见 plan / docs/audit/MULTI_AGENT_GROUPCHAT_RESEARCH_2026.md）：
* **高复用**：每个 agent 的回合 = 复用 ``AgentLoop.run_turn(room_session_id, ctx)``。
  事件顶层自带该 agent 的 id，自动落到房间 session → 一个 WS 收全、按讲者渲染。
* **共享 transcript**：Orchestrator 持有权威 transcript，每个回合把「房间用途 +
  近况(带讲者名牌) + 该你发言」合成进 user_message（= AutoGen 广播共享消息列表）。
* **无状态回合**：每次调 agent 前清掉它在房间 session 的私有 history，使它**只**看见
  注入的完整 transcript（避免双份上下文 + 角色错位）。
* **选讲者**：``round_robin``（按顺序轮）/ ``supervisor``（一次 LLM 调用按用途动态点）。

依赖全部以可注入参数传入（get_agent_loop / llm_select），便于单测 stub。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from xmclaw.daemon.group_room import GroupRoom

# 类型别名：拿某 agent 的运行时（AgentLoop，鸭子类型——只需 .run_turn + ._histories）
GetAgentLoop = Callable[[str], Any]
# supervisor 选讲者用：async (prompt) -> str（返回参与者 id 或 USER/DONE）
LlmSelect = Callable[[str], Awaitable[str]]

_STOP_TOKENS = {"USER", "DONE", "STOP", "END", "用户", "结束"}


class GroupOrchestrator:
    def __init__(
        self,
        room: GroupRoom,
        get_agent_loop: GetAgentLoop,
        *,
        llm_select: LlmSelect | None = None,
        transcript_window: int = 14,
    ) -> None:
        self.room = room
        self._get_loop = get_agent_loop
        self._llm_select = llm_select
        self._win = transcript_window
        self.transcript: list[dict[str, str]] = []  # [{speaker, text}]
        self._rr_idx = 0
        self._last_speaker: str | None = None

    # ── transcript 渲染 ──
    def _recent(self, n: int | None = None) -> str:
        rows = self.transcript[-(n or self._win):]
        out = []
        for r in rows:
            who = "用户" if r["speaker"] == "user" else r["speaker"]
            out.append(f"[{who}] {r['text']}")
        return "\n".join(out)

    # ── 选讲者 ──
    async def _select_speaker(self) -> str:
        ps = list(self.room.participants)
        if not ps:
            return "USER"
        if self.room.policy == "supervisor" and self._llm_select is not None:
            choice = (await self._llm_select(self._supervisor_prompt(ps)) or "").strip()
            if choice.upper() in _STOP_TOKENS or choice in _STOP_TOKENS:
                return "USER"
            # 容错匹配：选中的 id 出现在输出里即可
            for p in ps:
                if p.lower() in choice.lower():
                    return p
            return "USER"  # 没点明确参与者 → 交回用户
        # round_robin（也是 supervisor 无 LLM 时的兜底）
        nxt = ps[self._rr_idx % len(ps)]
        self._rr_idx += 1
        return nxt

    def _supervisor_prompt(self, ps: list[str]) -> str:
        return (
            "你是一个多 agent 群聊的【主持人】。只负责挑下一个该发言的参与者。\n"
            f"房间用途：{self.room.purpose or '（自由讨论）'}\n"
            f"参与者：{', '.join(ps)}\n\n"
            f"最近对话：\n{self._recent()}\n\n"
            "根据用途与对话进展，决定下一个最该发言的参与者。"
            "只输出一个参与者 id（精确）；若该把发言权交回用户、或讨论已自然结束，"
            "只输出 USER。不要解释。"
        )

    # ── 单个 agent 的回合上下文 ──
    def _turn_context(self, speaker: str) -> str:
        others = [p for p in self.room.participants if p != speaker]
        parts = []
        if self.room.purpose:
            parts.append(f"【群聊用途】{self.room.purpose}")
        parts.append(
            f"【你是】{speaker}（群聊参与者）。其他参与者：{', '.join(others) or '（无）'}。"
            "请保持你自己的人格与立场。"
        )
        parts.append(f"【房间近况】\n{self._recent()}")
        parts.append(
            "【你的回合】简洁地推进讨论：回应他人、补充信息、提出异议或下一步。"
            "不要复述别人已说的内容；只说你这一轮要加的。"
        )
        return "\n\n".join(parts)

    # ── 主循环 ──
    async def run_round(
        self,
        user_message: str,
        *,
        on_speaker: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """处理一条用户消息：让 agent 之间轮转发言，直到交回用户 / 达上限。

        返回 ``{"speakers": [...], "rounds": n, "transcript": [...]}``。
        实时事件由各 agent 的 run_turn 经 bus → 房间 session → WS 推出。
        """
        if user_message:
            self.transcript.append({"speaker": "user", "text": user_message})
        spoke: list[str] = []
        session_id = self.room.session_id
        for _ in range(max(1, int(self.room.max_rounds))):
            speaker = await self._select_speaker()
            if speaker in _STOP_TOKENS:
                break
            loop = self._get_loop(speaker)
            if loop is None:
                continue
            if on_speaker is not None:
                try:
                    await on_speaker(speaker)
                except Exception:  # noqa: BLE001 — UI hint must never break the round
                    pass
            # 无状态回合：清掉该 agent 在房间 session 的私有 history，
            # 让它只看注入的完整 transcript（避免双份上下文/角色错位）。
            try:
                histories = getattr(loop, "_histories", None)
                if isinstance(histories, dict):
                    histories.pop(session_id, None)
            except Exception:  # noqa: BLE001
                pass
            try:
                result = await loop.run_turn(session_id, self._turn_context(speaker))
            except Exception as exc:  # noqa: BLE001 — one agent failing != round dead
                self.transcript.append({"speaker": speaker, "text": f"（{speaker} 出错：{exc}）"})
                continue
            text = (getattr(result, "text", "") or "").strip()
            if text:
                self.transcript.append({"speaker": speaker, "text": text})
            spoke.append(speaker)
            self._last_speaker = speaker
        return {"speakers": spoke, "rounds": len(spoke), "transcript": list(self.transcript)}
