"""One daemon-owned Workbench runtime sharing the existing Engine and Store."""

from .agents import Agents
from .devices import Devices
from .events import Journal
from .execution import EngineAgentAdapter
from .planning import Planning
from .retention import Retention
from .scheduler import Scheduler
from .service import WorkbenchService


class WorkbenchRuntime:
    def __init__(self, engine):
        if getattr(engine, "workbench_runtime", None) is not None:
            raise RuntimeError("Engine already owns a Workbench runtime")
        self.engine = engine
        engine.workbench_runtime = self
        self.service = WorkbenchService(engine.store)
        self.service.skills = engine.skills
        self.service.seed_defaults()
        self.journal = Journal(self.service)
        self.planning = Planning(self)
        self.agents = Agents(self)
        self.service.agents = self.agents
        self.execution = EngineAgentAdapter(self)
        self.service.execution = self.execution
        self.scheduler = Scheduler(self)
        self.started = False
        self.lan_error = None
        self.devices = Devices(self)
        self.retention = Retention(self)

    async def start(self, *, dispatcher=False):
        self.planning.reconcile_startup()
        self.started = True
        await self.agents.start()
        if dispatcher:
            await self.scheduler.start()

    async def close(self):
        await self.scheduler.close()
        await self.planning.close()
        self.started = False
        await self.agents.close()
