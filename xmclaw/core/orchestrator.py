"""Agent orchestrator: manages agent instances and routes requests."""
from xmclaw.core.agent_loop import AgentLoop
from xmclaw.llm.router import LLMRouter
from xmclaw.tools.registry import ToolRegistry
from xmclaw.memory.manager import MemoryManager
from xmclaw.utils.log import logger


class AgentOrchestrator:
    def __init__(self):
        self.llm = LLMRouter()
        self.tools = ToolRegistry(llm_router=self.llm)
        self.memory = MemoryManager(llm_router=self.llm)
        self.agents: dict[str, AgentLoop] = {}

    async def initialize(self) -> None:
        logger.info("orchestrator_initializing")
        await self.tools.load_all()
        await self.memory.initialize()
        logger.info("orchestrator_ready")

    async def shutdown(self) -> None:
        logger.info("orchestrator_shutting_down")
        await self.memory.close()

    async def run_agent(self, agent_id: str, user_input: str):
        if agent_id not in self.agents:
            self.agents[agent_id] = AgentLoop(
                agent_id=agent_id,
                llm_router=self.llm,
                tools=self.tools,
                memory=self.memory,
            )
        agent = self.agents[agent_id]
        async for chunk in agent.run(user_input):
            yield chunk
