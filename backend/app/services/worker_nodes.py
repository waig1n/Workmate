"""Bounded health snapshots; task admission never performs network I/O."""
import asyncio
import time
import httpx

PROBE_INTERVAL = 5
PROBE_TIMEOUT = 3
STALE_AFTER = 15


class WorkerNodeDirectory:
    def __init__(self, nodes, token, *, transport=None, clock=time.monotonic):
        self.nodes = {node.storage_node_id: node for node in nodes}
        self.token, self.transport, self.clock = token, transport, clock
        self._ready = {}
        self._task = None
        self._client = None

    def available(self, node_id):
        checked = self._ready.get(node_id)
        return checked is not None and self.clock() - checked < STALE_AFTER

    def candidates(self):
        return tuple(node_id for node_id, node in self.nodes.items()
                     if node.enabled and self.available(node_id))

    async def refresh(self):
        if not self.token:
            self._ready.clear()
            return
        async def probe(node_id, node):
            try:
                response = await self._client.get(str(node.base_url).rstrip('/') + '/workmate/node')
                response.raise_for_status()
                value = response.json()
                if value.get('role') != 'worker' or value.get('ready') is not True or value.get('storage_node_id') != node_id or value.get('artifact_storage_backend') != 's3':
                    raise ValueError('Runtime identity or readiness mismatch')
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                self._ready.pop(node_id, None)
            else:
                self._ready[node_id] = self.clock()
        await asyncio.gather(*(probe(key, node) for key, node in self.nodes.items()))

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=PROBE_TIMEOUT, transport=self.transport,
            headers={'X-Workmate-Internal-Token': self.token}, follow_redirects=False)
        try:
            await self.refresh()
        except BaseException:
            await self._client.aclose()
            raise
        async def monitor():
            while True:
                await asyncio.sleep(PROBE_INTERVAL)
                await self.refresh()
        self._task = asyncio.create_task(monitor(), name='worker-node-health')
        return self

    async def __aexit__(self, *args):
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        await self._client.aclose()
