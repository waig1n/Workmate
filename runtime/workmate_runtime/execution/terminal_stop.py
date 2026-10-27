"""Settle a late Stop from durable terminal proof; never construct or call Core."""
from workmate_runtime.transport.terminal_stop import TerminalStopStore


class TerminalStopController:
    def __init__(self,pool):
        self.pool=pool

    async def settle(self,command_id,worker_id,storage_node_id):
        async with self.pool.acquire() as db:
            return await TerminalStopStore(db).settle(command_id,worker_id,storage_node_id)
