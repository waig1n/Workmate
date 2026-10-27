"""Session MCP ownership: transport enter, RPC and exit share one asyncio Task.

This module intentionally uses no Product or App imports so a gateway can ship
it inside the sandbox. A cancelled HTTP waiter never cancels the actor's RPC.
"""
import asyncio


class MCPActorError(RuntimeError):
    pass


class SessionMCPActor:
    def __init__(self, client):
        self.client = client
        self._queue = asyncio.Queue()
        self._ready = asyncio.get_running_loop().create_future()
        self._closing = False
        self._failed = False
        self._active = 0
        self._task = asyncio.create_task(self._run())

    @property
    def busy(self):
        return self._active > 0

    @property
    def failed(self):
        return self._failed

    async def ready(self):
        await asyncio.shield(self._ready)

    @staticmethod
    def _consume(future):
        if not future.cancelled():
            future.exception()

    async def submit(self, operation, *args):
        if operation not in {'tools', 'call', 'close'}:
            raise MCPActorError('Unknown MCP actor operation')
        await self.ready()
        if self._failed or self._closing or self._task.done():
            raise MCPActorError('MCP Session infrastructure is unavailable')
        if operation == 'close':
            if self.busy:
                raise MCPActorError('MCP calls must finish before Session closure')
            self._closing = True
        else:
            self._active += 1
        result = asyncio.get_running_loop().create_future()
        result.add_done_callback(self._consume)
        self._queue.put_nowait((operation, args, result))
        return await asyncio.shield(result)

    async def _close(self):
        # Core.close clears its private identity even when AsyncExitStack fails.
        # Keep the exact stack/client on failure for diagnosis; a failed exit is
        # unknown and cannot be retried into a false proof of successful close.
        if self.client.is_stateful and self.client.is_connected:
            stack = self.client._stack
            await stack.aclose()
            self.client._client = None
            self.client._stack = None
            self.client._session = None
            self.client._is_connected = False

    async def _run(self):
        try:
            if self.client.is_stateful:
                await self.client.connect()
            await self.client.list_raw_tools()
        except BaseException:
            self._failed = True
            self._ready.set_exception(MCPActorError('MCP Session initialization failed'))
            self._ready.add_done_callback(self._consume)
            return
        self._ready.set_result(None)
        while True:
            operation, args, result = await self._queue.get()
            try:
                if operation == 'close':
                    await self._close()
                    value = None
                elif operation == 'tools':
                    value = await self.client.list_raw_tools()
                else:
                    tool = await self.client.get_tool(args[0])
                    value = await tool(**args[1])
                result.set_result(value)
            except BaseException:
                # A transport exception does not establish that remote work
                # ended. Retain Session identity and disallow replacement.
                self._failed = True
                result.set_exception(MCPActorError('MCP operation has an unknown outcome'))
            finally:
                if operation != 'close':
                    self._active -= 1
            if operation == 'close':
                return
            if self._failed:
                while not self._queue.empty():
                    queued, _, pending = self._queue.get_nowait()
                    if queued != 'close':
                        self._active -= 1
                    pending.set_exception(MCPActorError('MCP Session infrastructure is unavailable'))
                return
