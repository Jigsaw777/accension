"""Coalesced local SSE fanout, with one low-frequency cross-process revision check."""
import asyncio
import json


class SavingsStream:
    def __init__(self, engine):
        self.engine = engine
        self.queues = set()
        self.event = asyncio.Event()
        self.loop = None
        self.last_revision = -1

    def wake(self):
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.event.set)

    async def watch(self):
        self.loop = asyncio.get_running_loop()
        self.engine.store.savings.on_change = self.wake
        try:
            while True:
                try:
                    await asyncio.wait_for(self.event.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
                self.event.clear()
                # One queue item per subscriber; intermediate updates are replaced.
                await asyncio.sleep(.25)
                self.engine.store.economics("recover_abandoned")
                revision = self.engine.store.economics("revision")
                if revision != self.last_revision and self.queues:
                    state = self.engine.store.economics("state")
                    if state:
                        packet = self.packet(state)
                        for queue in tuple(self.queues):
                            if queue.full():
                                queue.get_nowait()
                            queue.put_nowait(packet)
                        self.last_revision = revision
        finally:
            self.engine.store.savings.on_change = None
            self.loop = None

    @staticmethod
    def packet(state):
        return "event: savings.updated\nid: " + str(state["revision"]) + "\ndata: " + json.dumps(state, separators=(",", ":")) + "\n\n"

    async def events(self, request):
        queue = asyncio.Queue(maxsize=1)
        self.queues.add(queue)
        try:
            state = self.engine.store.economics("state")
            if state:
                yield self.packet(state)
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            self.queues.discard(queue)
