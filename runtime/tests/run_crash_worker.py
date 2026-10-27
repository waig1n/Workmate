"""Real Streams Worker fixture whose one filesystem effect precedes a blocked tool."""
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolChunk
from test_core_engine import ConfirmTool
from test_execution_controller import prepare_reply
from test_parked_stop import quiescent
from run_control_worker import stream_deltas
from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.execution.service import WorkerService, WorkerBindings
from workmate_runtime.execution.command_queue import CommandQueueSource
from workmate_runtime.execution.realtime_store import RealtimeWriterFactory
from workmate_product.coordination.command_queue import RedisCommandQueue
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.realtime.composition import create_realtime_log
from workmate_product.realtime.log_manager import EventLogManager


class BlockingEffect(ConfirmTool):
    async def check_permissions(self, *args):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message='isolated fixture')

    async def __call__(self, **kwargs):
        self.calls += 1
        with Path(os.environ['WORKMATE_TEST_EFFECT_FILE']).open('a') as file:
            file.write('one effect\n')
        yield ToolChunk(content=[TextBlock(text='effect committed; result pending')], is_last=False)
        print('tool_waiting', flush=True)
        await asyncio.Event().wait()


async def main():
    dsn = os.environ['WORKMATE_TEST_POSTGRES_DSN']
    namespace = os.environ['WORKMATE_TEST_REALTIME_NAMESPACE']
    pool = await asyncpg.create_pool(dsn, server_settings={'search_path':sys.argv[1]})
    engine = create_async_engine(dsn.replace('postgresql://','postgresql+asyncpg://'),
        connect_args={'server_settings':{'search_path':sys.argv[1]}})
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    redis=Redis.from_url(os.environ['WORKMATE_TEST_REDIS_URL'],decode_responses=True,retry=Retry(NoBackoff(),0),retry_on_timeout=False)
    policy=RealtimePolicy()
    log=create_realtime_log(redis,namespace=namespace,policy=policy)
    controller=RunningExecutionController(pool,verify_cleanup=quiescent,lease_seconds=2)
    controller.realtime_writer_factory=RealtimeWriterFactory(sessions,
        log_manager=EventLogManager(sessions,log),log=log,policy=policy)
    tool=BlockingEffect()
    prepare,_=prepare_reply([[TextBlock(text='persisted partial answer'),ToolCallBlock(id='a',name=tool.name,input='{}')]],tool=tool)
    async def start(*args):
        prepared=await prepare(*args)
        stream_deltas(prepared.engine)
        return prepared
    async def unused(*args,**kwargs):
        raise AssertionError('The interrupted tool must never be resumed by this test')
    source=CommandQueueSource(RedisCommandQueue(redis,namespace=namespace),sessions,pending_idle_ms=50)
    worker=WorkerService(pool,'node',WorkerBindings(start,unused,unused,unused,quiescent),
        controller=controller,command_source=source,capacity=1,lease_seconds=2,drain_seconds=0)
    shutdown=asyncio.Event()
    async def input_shutdown():
        await asyncio.to_thread(sys.stdin.readline)
        shutdown.set()
    reader=asyncio.create_task(input_shutdown())
    try:
        print('ready',flush=True)
        await worker.serve(shutdown)
        print(json.dumps({'tool_calls':tool.calls}),flush=True)
    finally:
        reader.cancel()
        await asyncio.gather(reader,return_exceptions=True)
        await redis.aclose()
        await engine.dispose()
        await pool.close()


if __name__=='__main__':
    asyncio.run(main())
