"""Agent orchestrator: manages agent instances and routes requests."""
from xmclaw.core.agent_loop import AgentLoop
from xmclaw.core.event_bus import EventBus, Event, EventType, get_event_bus
from xmclaw.llm.router import LLMRouter
from xmclaw.tools.registry import ToolRegistry
from xmclaw.memory.manager import MemoryManager
from xmclaw.genes.manager import GeneManager
from xmclaw.utils.log import logger


class AgentOrchestrator:
    """Multi-agent orchestration system with event-driven communication."""

    def __init__(self):
        self.llm = LLMRouter()
        self.tools = ToolRegistry(llm_router=self.llm)
        self.memory = MemoryManager(llm_router=self.llm)
        self.gene_manager = GeneManager()
        self.agents: dict[str, AgentLoop] = {}
        self._event_bus = get_event_bus()
        self._teams: dict[str, list[str]] = {}  # team_name -> [agent_ids]

    async def initialize(self) -> None:
        logger.info("orchestrator_initializing")
        await self.tools.load_all()
        await self.memory.initialize()
        AgentOrchestrator._tool_memory = self.memory
        # Share the tool registry with the evolution engine so skill reloads
        # update the same live registry that agents use.
        from xmclaw.tools.registry import ToolRegistry
        ToolRegistry.set_shared(self.tools)
        logger.info("orchestrator_ready")

    async def shutdown(self) -> None:
        logger.info("orchestrator_shutting_down")
        for agent_id in list(self.agents.keys()):
            await self._event_bus.publish(Event(
                event_type=EventType.AGENT_STOP,
                source="orchestrator",
                target=agent_id,
                payload={"agent_id": agent_id},
            ))
        await self.memory.close()

    # ── Single agent ──────────────────────────────────────────────────────────

    async def get_or_create_agent(self, agent_id: str) -> AgentLoop:
        """Get or create an agent instance. Used by integrations."""
        if agent_id not in self.agents:
            self.agents[agent_id] = AgentLoop(
                agent_id=agent_id,
                llm_router=self.llm,
                tools=self.tools,
                memory=self.memory,
            )
        return self.agents[agent_id]

    async def run_agent(self, agent_id: str, user_input: str):
        """Run a single agent and yield response chunks."""
        await self._event_bus.publish(Event(
            event_type=EventType.AGENT_START,
            source="orchestrator",
            target=agent_id,
            payload={"agent_id": agent_id, "input_preview": user_input[:100]},
        ))

        # Ensure agent exists
        if agent_id not in self.agents:
            self.agents[agent_id] = AgentLoop(
                agent_id=agent_id,
                llm_router=self.llm,
                tools=self.tools,
                memory=self.memory,
            )

        try:
            async for chunk in self.agents[agent_id].run(user_input):
                yield chunk
            await self._event_bus.publish(Event(
                event_type=EventType.AGENT_STOP,
                source="orchestrator",
                target=agent_id,
                payload={"agent_id": agent_id},
            ))
        except Exception as e:
            await self._event_bus.publish(Event(
                event_type=EventType.AGENT_ERROR,
                source="orchestrator",
                target=agent_id,
                payload={"agent_id": agent_id, "error": str(e)},
            ))
            raise

    # ── Multi-agent team ───────────────────────────────────────────────────────

    async def create_team(
        self,
        team_name: str,
        agent_ids: list[str],
        shared_memory: bool = True,
    ) -> dict[str, AgentLoop]:
        """
        Create a team of independent agents.

        Args:
            team_name: Name for this team
            agent_ids: List of agent IDs to create
            shared_memory: If True, agents share the same memory manager.
                          If False, each agent gets an isolated memory.

        Returns:
            Dict mapping agent_id -> AgentLoop instance
        """
        self._teams[team_name] = agent_ids
        memory = self.memory if shared_memory else MemoryManager(llm_router=self.llm)

        for agent_id in agent_ids:
            if agent_id not in self.agents:
                self.agents[agent_id] = AgentLoop(
                    agent_id=agent_id,
                    llm_router=self.llm,
                    tools=self.tools,
                    memory=memory,
                )
            await self._event_bus.publish(Event(
                event_type=EventType.AGENT_START,
                source="team:" + team_name,
                target=agent_id,
                payload={"team": team_name},
            ))

        logger.info(f"team_created", team=team_name, agents=agent_ids)
        return {aid: self.agents[aid] for aid in agent_ids if aid in self.agents}

    async def delegate(
        self,
        parent_agent_id: str,
        task: str,
        sub_agent_id: str | None = None,
    ) -> tuple[str, str]:
        """
        Delegate a task to a sub-agent.

        Args:
            parent_agent_id: The agent creating the delegation
            task: Task description
            sub_agent_id: Optional specific sub-agent ID; auto-generated if not provided

        Returns:
            (sub_agent_id, status)
        """
        if sub_agent_id is None:
            sub_agent_id = f"{parent_agent_id}_sub_{len(self.agents)}"

        await self._event_bus.publish(Event(
            event_type=EventType.TASK_ASSIGNED,
            source=parent_agent_id,
            target=sub_agent_id,
            payload={"task": task, "parent": parent_agent_id},
        ))

        if sub_agent_id not in self.agents:
            self.agents[sub_agent_id] = AgentLoop(
                agent_id=sub_agent_id,
                llm_router=self.llm,
                tools=self.tools,
                memory=self.memory,
            )

        result_chunks = []
        async for chunk in self.agents[sub_agent_id].run(task):
            result_chunks.append(chunk)

        await self._event_bus.publish(Event(
            event_type=EventType.TASK_COMPLETED,
            source=sub_agent_id,
            target=parent_agent_id,
            payload={"parent": parent_agent_id, "sub_agent": sub_agent_id},
        ))

        return sub_agent_id, "".join(result_chunks)

    async def coordinate(self, team_name: str) -> dict[str, str]:
        """
        Get status of all agents in a team.

        Args:
            team_name: Name of the team

        Returns:
            Dict of agent_id -> status
        """
        agent_ids = self._teams.get(team_name, [])
        return {
            agent_id: "running" if agent_id in self.agents else "unknown"
            for agent_id in agent_ids
        }

    async def run_team(
        self,
        team_name: str,
        task: str,
        parallel: bool = True,
    ) -> dict[str, str]:
        """
        Run a task across all agents in a team.

        Args:
            team_name: Name of the team
            task: Task description (broadcast to all agents)
            parallel: If True, run all agents simultaneously. If False, run sequentially.

        Returns:
            Dict of agent_id -> result string
        """
        agent_ids = self._teams.get(team_name, [])
        if not agent_ids:
            return {}

        results: dict[str, str] = {}

        if parallel:
            import asyncio
            async def run_one(aid: str) -> tuple[str, str]:
                try:
                    chunks = []
                    async for chunk in self.agents[aid].run(task):
                        chunks.append(chunk)
                    return aid, "".join(chunks)
                except Exception as e:
                    return aid, f"[Error: {e}]"
            gathered = await asyncio.gather(*[run_one(aid) for aid in agent_ids], return_exceptions=True)
            for item in gathered:
                if isinstance(item, Exception):
                    continue
                aid, result = item
                results[aid] = result
        else:
            for aid in agent_ids:
                try:
                    chunks = []
                    async for chunk in self.agents[aid].run(task):
                        chunks.append(chunk)
                    results[aid] = "".join(chunks)
                except Exception as e:
                    results[aid] = f"[Error: {e}]"

        return results

    async def merge_results(self, results: dict[str, str], strategy: str = "concat") -> str:
        """
        Merge results from multiple agents into a single response.

        Args:
            results: Dict of agent_id -> result string
            strategy: Merge strategy — 'concat' (newline separated), 'first' (return first non-empty), 'vote' (majority)

        Returns:
            Merged result string
        """
        non_empty = {k: v for k, v in results.items() if v and not v.startswith("[Error")}
        if not non_empty:
            return "No successful results."

        if strategy == "first":
            return next(iter(non_empty.values()))

        if strategy == "concat":
            lines = []
            for aid, result in non_empty.items():
                lines.append(f"--- {aid} ---")
                lines.append(result.strip())
            return "\n".join(lines)

        if strategy == "vote":
            # Simple majority: count identical answers
            from collections import Counter
            answers = [r.strip() for r in non_empty.values() if len(r.strip()) < 500]
            if not answers:
                return next(iter(non_empty.values()))
            counted = Counter(answers)
            return counted.most_common(1)[0][0]

        return "\n".join(non_empty.values())

    # ── Event bus access ───────────────────────────────────────────────────────

    def get_event_bus(self) -> EventBus:
        """Access the event bus for custom subscriptions."""
        return self._event_bus

    def subscribe(
        self,
        event_type: str,
        handler,
    ) -> str:
        """Subscribe to events. Returns subscription ID."""
        return self._event_bus.subscribe(event_type, handler)

    async def publish(self, event: Event) -> int:
        """Publish an event to all subscribers."""
        return await self._event_bus.publish(event)
