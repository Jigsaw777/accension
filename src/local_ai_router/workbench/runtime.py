"""One daemon-owned Workbench runtime sharing the existing Engine and Store."""

import asyncio
import logging

from .agents import Agents
from .delivery import Delivery
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
        self.delivery = Delivery(self)
        self.started = False
        self.projections = None
        self.lan_error = None
        self.devices = Devices(self)
        self.retention = Retention(self)

    async def start(self, *, dispatcher=False):
        if self.started:
            return
        from .activity import backfill_activity

        backfill_activity(self.service.store)
        from .metrics import backfill_metrics

        backfill_metrics(self)
        self.planning.reconcile_startup()
        self.started = True
        self.projections = asyncio.create_task(self.projection_loop())
        await self.agents.start()
        if dispatcher:
            await self.scheduler.start()

    async def projection_loop(self):
        from .activity import backfill_activity
        from .metrics import backfill_metrics

        while self.started:
            await asyncio.sleep(1)
            try:
                backfill_activity(self.service.store)
                backfill_metrics(self)
            except Exception:
                logging.getLogger(__name__).exception("Workbench projection backfill failed")

    async def close(self):
        self.started = False
        if self.projections:
            self.projections.cancel()
            await asyncio.gather(self.projections, return_exceptions=True)
            self.projections = None
        await self.scheduler.close()
        await self.planning.close()
        self.started = False
        await self.agents.close()
