"""Evolution scheduler integrated with APScheduler."""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from xmclaw.evolution.engine import EvolutionEngine
from xmclaw.daemon.config import DaemonConfig
from xmclaw.utils.log import logger


class EvolutionScheduler:
    def __init__(self, agent_id: str = "default"):
        self.agent_id = agent_id
        self.engine = EvolutionEngine(agent_id)
        self.scheduler = AsyncIOScheduler()
        self.config = DaemonConfig.load()
        self.running = False

    async def start(self) -> None:
        await self.engine.initialize()
        interval = self.config.evolution.get("interval_minutes", 30)
        self.scheduler.add_job(
            self._run_cycle,
            "interval",
            minutes=interval,
            id="evolution_cycle",
            replace_existing=True,
        )
        self.scheduler.start()
        self.running = True
        logger.info("evolution_scheduler_started", interval_minutes=interval)

    def stop(self) -> None:
        self.scheduler.shutdown()
        self.running = False
        logger.info("evolution_scheduler_stopped")

    async def _run_cycle(self) -> None:
        try:
            result = await self.engine.run_cycle()
            logger.info("evolution_cycle_completed", result=result)
        except Exception as e:
            logger.error("evolution_cycle_failed", error=str(e))
