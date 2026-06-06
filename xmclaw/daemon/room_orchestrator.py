"""RoomOrchestrator — 统一的多 agent 编排内核（4 策略）.

Group 重做 (2026-06-06)。用户"都要"：一个房间可按 4 种**真实存在、彼此不同**的
范式编排参与者，全部**限定在 room.participants 内**、用**结构化人格**(role/goal/
backstory) 做选择/分派、明确终止。对照调研见
``docs/audit/MULTI_AGENT_LOGIC_AUDIT_2026.md``。

策略：
* ``chat``       — 群聊（AutoGen GroupChat）：共享 transcript；每轮 LLM 选下一个讲者
  （排除上一个）或 DONE；被选 agent 看到**自上次发言以来的增量**（不是整段 blob，
  也不清历史）→ 各 agent 累积自己的会话视角。
* ``sequential`` — 固定流水线（CrewAI sequential / MetaGPT SOP）：按 participants
  顺序 A→B→C，每个拿到目标 + 前序产出，接力传递。顺序你预先定，不靠 LLM。
* ``supervisor`` — 主管派活（CrewAI hierarchical / LangGraph supervisor）：主管 LLM
  按角色把子任务动态分给专才，多轮，主管判 DONE，末了 LLM 合成。
* ``autonomous`` — 目标驱动（Magentic-One）：任务台账 + 进度台账（内循环 5 问：完成没/
  打转没/有进展没/谁下一个/给什么指令），卡住计数→重规划，直到完成/上限。

依赖全部注入（get_agent_loop / llm_complete / get_persona / publish），便于单测 stub，
**不碰** app.py / factory.py / AgentLoop。无 LLM 时各策略优雅降级（chat→轮流，
supervisor/autonomous→顺序兜底）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from xmclaw.daemon.group_room import GroupRoom

# 拿某 agent 的运行时（AgentLoop，鸭子类型：.run_turn(session_id, msg) + ._histories dict）
GetAgentLoop = Callable[[str], Any]
# 通用 LLM 调用：async (system:str, user:str) -> str
LlmComplete = Callable[[str, str], Awaitable[str]]
# 拿某 agent 的结构化人格：(agent_id) -> {"role","goal","backstory","style"} 任意子集
GetPersona = Callable[[str], dict[str, Any]]
# 推事件到房间 session：async (event_type:str, payload:dict) -> None
PublishFn = Callable[[str, dict[str, Any]], Awaitable[None]]
# UI 提示：async (speaker:str) -> None（"X 正在输入"）
OnSpeaker = Callable[[str], Awaitable[None]]

_STOP_TOKENS = {"USER", "DONE", "STOP", "END", "FINISH", "用户", "结束", "完成"}


class RoomOrchestrator:
    def __init__(
        self,
        room: GroupRoom,
        get_agent_loop: GetAgentLoop,
        *,
        llm_complete: LlmComplete | None = None,
        get_persona: GetPersona | None = None,
        publish: PublishFn | None = None,
        transcript_window: int = 16,
        max_stall: int = 2,
    ) -> None:
        self.room = room
        self._get_loop = get_agent_loop
        self._llm = llm_complete
        self._get_persona = get_persona
        self._publish = publish
        self._win = transcript_window
        self._max_stall = max(1, int(max_stall))
        # 权威共享 transcript：[{speaker, text}]（speaker=="user" 表示用户）
        self.transcript: list[dict[str, str]] = []
        self._rr_idx = 0
        self._last_speaker: str | None = None
        # chat 策略：每个 agent "看到 transcript 到第几条"，用于喂增量（AutoGen 式累积）
        self._seen: dict[str, int] = {}

    # ════════════════════════════════════════════════════════════════════
    # 公共入口：按策略分派
    # ════════════════════════════════════════════════════════════════════
    async def run(
        self,
        user_message: str = "",
        *,
        on_speaker: OnSpeaker | None = None,
    ) -> dict[str, Any]:
        if user_message:
            self.transcript.append({"speaker": "user", "text": user_message})
        strategy = self.room.resolve_strategy()
        await self._emit("room_run_started", {
            "strategy": strategy,
            "participants": list(self.room.participants),
            "purpose": self.room.purpose,
        })
        handler = {
            "chat": self._run_chat,
            "sequential": self._run_sequential,
            "supervisor": self._run_supervisor,
            "autonomous": self._run_autonomous,
        }.get(strategy, self._run_chat)
        out = await handler(user_message, on_speaker)
        out.setdefault("strategy", strategy)
        out["transcript"] = list(self.transcript)
        await self._emit("room_run_done", {
            "strategy": strategy,
            "speakers": out.get("speakers", []),
            "ok": out.get("ok", True),
        })
        return out

    # ════════════════════════════════════════════════════════════════════
    # 策略 1：群聊（AutoGen GroupChat）
    # ════════════════════════════════════════════════════════════════════
    async def _run_chat(self, user_message: str, on_speaker: OnSpeaker | None) -> dict[str, Any]:
        spoke: list[str] = []
        for _ in range(max(1, int(self.room.max_rounds))):
            speaker = await self._select_chat_speaker()
            if speaker is None:
                break
            ctx = self._chat_context(speaker)
            text = await self._run_agent(speaker, ctx, on_speaker)
            spoke.append(speaker)
            self._last_speaker = speaker
        return {"speakers": spoke, "rounds": len(spoke), "ok": True}

    async def _select_chat_speaker(self) -> str | None:
        ps = list(self.room.participants)
        if not ps:
            return None
        # 主持人 LLM 自动选讲者（排除上一个发言者）
        if self._llm is not None and len(ps) > 1:
            choice = await self._ask_llm(
                self._sys_moderator(),
                self._chat_select_prompt(ps),
            )
            tok = (choice or "").strip()
            if self._is_stop(tok):
                return None
            picked = self._match_participant(tok, ps, exclude=self._last_speaker)
            if picked:
                return picked
            # LLM 没点明确参与者 → 自然结束，交回用户
            return None
        # 无 LLM / 单参与者：轮流（排除刚说过的）
        if len(ps) > 1 and self._last_speaker in ps:
            start = (ps.index(self._last_speaker) + 1) % len(ps)
            self._rr_idx = start
        nxt = ps[self._rr_idx % len(ps)]
        self._rr_idx += 1
        return nxt

    def _chat_select_prompt(self, ps: list[str]) -> str:
        roster = "\n".join(f"- {p}：{self._persona_brief(p)}" for p in ps)
        prev = f"（上一个发言者是 {self._last_speaker}，本轮请勿再选它）" if self._last_speaker else ""
        return (
            f"群聊用途：{self.room.purpose or '（自由讨论）'}\n"
            f"参与者及人设：\n{roster}\n\n"
            f"最近对话：\n{self._recent()}\n\n"
            f"请挑下一个最该发言、最能推进讨论的参与者。{prev}\n"
            "只输出一个参与者 id（精确匹配）。若讨论已自然结束/该交回用户，只输出 DONE。不要解释。"
        )

    def _chat_context(self, speaker: str) -> str:
        """喂给被选 agent 的上下文 = 自它上次发言以来的【增量】（不清历史，
        让它在自己的 session history 里累积——AutoGen 式各自累积视角）。"""
        seen = self._seen.get(speaker, 0)
        delta = self.transcript[seen:]
        self._seen[speaker] = len(self.transcript)
        others = [p for p in self.room.participants if p != speaker]
        parts: list[str] = []
        if seen == 0:  # 第一次发言：交代房间设定
            if self.room.purpose:
                parts.append(f"【群聊用途】{self.room.purpose}")
            parts.append(
                f"【你是】{speaker}。其他参与者：{', '.join(others) or '（无）'}。"
                "保持你自己的人格与立场，用第一人称发言。"
            )
        new_lines = self._format_rows(delta) or "（无新消息）"
        parts.append(f"【新进展】\n{new_lines}")
        parts.append(
            "【你的回合】基于上面新进展简洁推进：回应、补充、提异议或给下一步。"
            "不要复述别人已说的，只说你这轮要加的。"
        )
        return "\n\n".join(parts)

    # ════════════════════════════════════════════════════════════════════
    # 策略 2：固定流水线（CrewAI sequential / MetaGPT）
    # ════════════════════════════════════════════════════════════════════
    async def _run_sequential(self, user_message: str, on_speaker: OnSpeaker | None) -> dict[str, Any]:
        ps = list(self.room.participants)
        goal = self._goal(user_message)
        if not ps or not goal:
            return {"speakers": [], "ok": False, "error": "empty participants or goal",
                    "result": ""}
        spoke: list[str] = []
        prior = ""
        for speaker in ps:
            ctx = self._sequential_context(speaker, goal, prior)
            text = await self._run_agent(speaker, ctx, on_speaker)
            prior += f"\n【{speaker} 的产出】\n{text}\n"
            spoke.append(speaker)
        return {"speakers": spoke, "ok": True, "result": prior.strip()}

    def _sequential_context(self, speaker: str, goal: str, prior: str) -> str:
        parts = [f"【流水线目标】{goal}",
                 f"【你的角色】{speaker}：{self._persona_brief(speaker)}"]
        if prior.strip():
            parts.append(f"【上游交付】{prior.strip()}")
        else:
            parts.append("【你是流水线第一棒】没有上游产出，请从目标直接开工。")
        parts.append("【你的职责】基于上游产出完成你这一环，产出供下一棒接力。聚焦你的环节，别越权重做别人的。")
        return "\n\n".join(parts)

    # ════════════════════════════════════════════════════════════════════
    # 策略 3：主管派活（CrewAI hierarchical / LangGraph supervisor）
    # ════════════════════════════════════════════════════════════════════
    async def _run_supervisor(self, user_message: str, on_speaker: OnSpeaker | None) -> dict[str, Any]:
        ps = list(self.room.participants)
        goal = self._goal(user_message)
        if not ps or not goal:
            return {"speakers": [], "ok": False, "error": "empty participants or goal",
                    "result": ""}
        # 无 LLM → 退化为顺序流水线（仍限定参与者内）
        if self._llm is None:
            return await self._run_sequential(user_message, on_speaker)
        spoke: list[str] = []
        progress: list[str] = []
        for _ in range(max(1, int(self.room.max_rounds))):
            decision = await self._ask_json(
                self._sys_manager(),
                self._supervisor_prompt(ps, goal, progress),
            )
            nxt = self._match_participant(str(decision.get("next", "")), ps)
            if not nxt or self._is_stop(str(decision.get("next", ""))):
                break
            instruction = str(decision.get("instruction", "")).strip() or goal
            ctx = self._worker_context(nxt, goal, instruction, progress)
            text = await self._run_agent(nxt, ctx, on_speaker)
            progress.append(f"【{nxt}】指令：{instruction}\n产出：{text}")
            spoke.append(nxt)
        result = await self._synthesize(goal, progress)
        return {"speakers": spoke, "ok": True, "result": result}

    def _supervisor_prompt(self, ps: list[str], goal: str, progress: list[str]) -> str:
        roster = "\n".join(f"- {p}：{self._persona_brief(p)}" for p in ps)
        done = "\n\n".join(progress[-6:]) or "（尚未开始）"
        return (
            f"总目标：{goal}\n"
            f"你手下的专才：\n{roster}\n\n"
            f"已完成的工作：\n{done}\n\n"
            "作为主管，决定下一步把哪个子任务派给哪个专才。"
            "输出 JSON：{\"next\": \"<专才 id，精确>\", \"instruction\": \"<给他的具体子任务>\"}。"
            "若总目标已达成、无需再派活，输出 {\"next\": \"DONE\"}。只输出 JSON。"
        )

    def _worker_context(self, worker: str, goal: str, instruction: str, progress: list[str]) -> str:
        ctx = [f"【总目标】{goal}",
               f"【你的角色】{worker}：{self._persona_brief(worker)}",
               f"【主管派给你的子任务】{instruction}"]
        if progress:
            ctx.append(f"【团队已有进展】\n{progress[-1]}")
        ctx.append("【要求】只完成主管派给你的子任务，给出可交付的结果。")
        return "\n\n".join(ctx)

    # ════════════════════════════════════════════════════════════════════
    # 策略 4：目标驱动·自主（Magentic-One 双台账 + 内循环 5 问 + 重规划）
    # ════════════════════════════════════════════════════════════════════
    async def _run_autonomous(self, user_message: str, on_speaker: OnSpeaker | None) -> dict[str, Any]:
        ps = list(self.room.participants)
        goal = self._goal(user_message)
        if not ps or not goal:
            return {"speakers": [], "ok": False, "error": "empty participants or goal",
                    "result": ""}
        if self._llm is None:
            return await self._run_sequential(user_message, on_speaker)
        task_ledger = await self._build_task_ledger(ps, goal)
        await self._emit("workflow_plan", {"task_ledger": task_ledger})
        spoke: list[str] = []
        progress: list[str] = []
        stall = 0
        for _ in range(max(1, int(self.room.max_rounds))):
            pl = await self._ask_json(
                self._sys_orchestrator(),
                self._progress_ledger_prompt(ps, goal, task_ledger, progress),
            )
            if self._truthy(pl.get("complete")):
                break
            # 卡住检测：在打转 / 无进展
            if self._truthy(pl.get("stalled")) or not self._truthy(pl.get("progress", True)):
                stall += 1
                if stall >= self._max_stall:
                    task_ledger = await self._replan(ps, goal, task_ledger, progress)
                    await self._emit("workflow_replan", {"task_ledger": task_ledger})
                    stall = 0
                    continue
            else:
                stall = 0
            nxt = self._match_participant(str(pl.get("next", "")), ps)
            if not nxt:
                break
            instruction = str(pl.get("instruction", "")).strip() or goal
            ctx = self._worker_context(nxt, goal, instruction, progress)
            text = await self._run_agent(nxt, ctx, on_speaker)
            progress.append(f"【{nxt}】{instruction} → {text}")
            spoke.append(nxt)
        result = await self._synthesize(goal, progress)
        return {"speakers": spoke, "ok": True, "result": result, "task_ledger": task_ledger}

    async def _build_task_ledger(self, ps: list[str], goal: str) -> str:
        roster = "\n".join(f"- {p}：{self._persona_brief(p)}" for p in ps)
        return await self._ask_llm(
            self._sys_orchestrator(),
            f"目标：{goal}\n可用 agent：\n{roster}\n\n"
            "建一份【任务台账】：列出 ①已知事实 ②待查事实 ③需推导的事实 "
            "④初步计划（拆成步骤，每步注明大概交给哪个 agent）。简洁分点。",
        )

    async def _replan(self, ps: list[str], goal: str, ledger: str, progress: list[str]) -> str:
        done = "\n".join(progress[-6:]) or "（无）"
        return await self._ask_llm(
            self._sys_orchestrator(),
            f"目标：{goal}\n原任务台账：\n{ledger}\n\n已尝试但卡住了：\n{done}\n\n"
            "团队在打转/无进展。反思原因，给出【修订后的任务台账】（新计划、换思路）。简洁分点。",
        )

    def _progress_ledger_prompt(self, ps: list[str], goal: str, ledger: str, progress: list[str]) -> str:
        roster = ", ".join(ps)
        done = "\n".join(progress[-6:]) or "（尚未开始）"
        return (
            f"目标：{goal}\n任务台账：\n{ledger}\n\n"
            f"可用 agent：{roster}\n进展记录：\n{done}\n\n"
            "回答【进度台账】5 问，输出 JSON："
            "{\"complete\": <目标是否已达成 true/false>, "
            "\"stalled\": <团队是否在打转/重复 true/false>, "
            "\"progress\": <相比上一步是否有前进 true/false>, "
            "\"next\": \"<下一个该干活的 agent id，精确；若已完成填 DONE>\", "
            "\"instruction\": \"<给他的具体指令>\"}。只输出 JSON。"
        )

    # ════════════════════════════════════════════════════════════════════
    # 公共：跑一个 agent 的回合
    # ════════════════════════════════════════════════════════════════════
    async def _run_agent(self, speaker: str, context: str, on_speaker: OnSpeaker | None) -> str:
        loop = self._get_loop(speaker)
        if loop is None:
            note = f"（{speaker} 不在线，跳过）"
            self.transcript.append({"speaker": speaker, "text": note})
            return ""
        if on_speaker is not None:
            try:
                await on_speaker(speaker)
            except Exception:  # noqa: BLE001 — UI 提示失败不该中断
                pass
        await self._emit("room_speaker", {"speaker": speaker})
        try:
            result = await loop.run_turn(self.room.session_id, context)
        except Exception as exc:  # noqa: BLE001 — 单个 agent 出错 != 整轮死
            err = f"（{speaker} 出错：{type(exc).__name__}: {exc}）"
            self.transcript.append({"speaker": speaker, "text": err})
            return ""
        text = (getattr(result, "text", "") or "").strip()
        if text:
            self.transcript.append({"speaker": speaker, "text": text})
        return text

    async def _synthesize(self, goal: str, progress: list[str]) -> str:
        body = "\n\n".join(progress)
        if not body:
            return ""
        if self._llm is None:
            return body
        return await self._ask_llm(
            "你负责把多个 agent 的产出汇总成给用户的最终答复。",
            f"目标：{goal}\n\n各 agent 产出：\n{body}\n\n"
            "综合成一份连贯、完整、直接面向用户的最终结果。",
        )

    # ════════════════════════════════════════════════════════════════════
    # 工具：人格 / transcript / LLM / 匹配
    # ════════════════════════════════════════════════════════════════════
    def _persona_brief(self, agent_id: str) -> str:
        if self._get_persona is None:
            return agent_id
        try:
            p = self._get_persona(agent_id) or {}
        except Exception:  # noqa: BLE001
            return agent_id
        role = str(p.get("role", "")).strip()
        goal = str(p.get("goal", "")).strip()
        bits = [b for b in (role, goal) if b]
        return " — ".join(bits) if bits else agent_id

    def _goal(self, user_message: str) -> str:
        return (self.room.purpose or "").strip() or (user_message or "").strip()

    def _recent(self, n: int | None = None) -> str:
        return self._format_rows(self.transcript[-(n or self._win):])

    @staticmethod
    def _format_rows(rows: list[dict[str, str]]) -> str:
        out = []
        for r in rows:
            who = "用户" if r["speaker"] == "user" else r["speaker"]
            out.append(f"[{who}] {r['text']}")
        return "\n".join(out)

    @staticmethod
    def _is_stop(tok: str) -> bool:
        t = (tok or "").strip()
        return t.upper() in _STOP_TOKENS or t in _STOP_TOKENS

    @staticmethod
    def _truthy(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "1", "是", "已完成")
        return bool(v)

    @staticmethod
    def _match_participant(text: str, ps: list[str], *, exclude: str | None = None) -> str | None:
        """从 LLM 输出里抽出一个参与者 id：先精确，再子串包含（最长优先），排除 exclude。"""
        t = (text or "").strip()
        cands = [p for p in ps if p != exclude] or list(ps)
        for p in cands:  # 精确
            if t == p:
                return p
        # 子串：transcript 里出现该 id（最长 id 优先，避免 'a' 命中 'analyst'）
        for p in sorted(cands, key=len, reverse=True):
            if p and p.lower() in t.lower():
                return p
        return None

    async def _ask_llm(self, system: str, user: str) -> str:
        if self._llm is None:
            return ""
        try:
            return (await self._llm(system, user) or "").strip()
        except Exception:  # noqa: BLE001 — LLM 失败不该让整轮崩
            return ""

    async def _ask_json(self, system: str, user: str) -> dict[str, Any]:
        raw = await self._ask_llm(system + " 只输出 JSON，不要 markdown 代码块。", user)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        if not raw:
            return {}
        s = raw.strip()
        # 剥 ```json ... ``` 围栏
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except (json.JSONDecodeError, TypeError):
            pass
        # 容错：抓第一个 {...} 块
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    # ── system prompts ──
    @staticmethod
    def _sys_moderator() -> str:
        return "你是多 agent 群聊的【主持人】，只负责挑下一个发言者，不替任何参与者发言。"

    @staticmethod
    def _sys_manager() -> str:
        return "你是多 agent 团队的【主管】，按每个专才的角色把子任务派给最合适的人。"

    @staticmethod
    def _sys_orchestrator() -> str:
        return ("你是 Magentic-One 式【编排器】，维护任务台账与进度台账，"
                "每步判断进展并决定下一个 agent 与指令。")

    # ── 事件 ──
    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._publish is None:
            return
        try:
            await self._publish(event_type, {"room_id": self.room.room_id, **payload})
        except Exception:  # noqa: BLE001 — 事件推送失败不该中断编排
            pass
