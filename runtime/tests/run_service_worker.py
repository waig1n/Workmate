"""Deterministic cross-process fixture for the autonomous Worker service."""
import asyncio
import json
import os
import sys

import asyncpg
from agentscope.message import TextBlock, ToolCallBlock
from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.execution.service import WorkerBindings
from polling_worker import WorkerService
from test_core_engine import ConfirmTool, make_engine
from test_execution_controller import prepare_reply
from test_parked_stop import quiescent
from workmate_runtime.permission_policy import ExecutionPermissionPolicy, ProductPermissionMiddleware
from run_control_worker import stream_deltas


async def main():
    pool = await asyncpg.create_pool(os.environ['WORKMATE_TEST_POSTGRES_DSN'],
        server_settings={'search_path':sys.argv[1]})
    from redis.asyncio import Redis
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff
    from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
    from workmate_product.config.realtime import RealtimePolicy
    from workmate_product.realtime.composition import create_realtime_log
    from workmate_product.realtime.log_manager import EventLogManager
    from workmate_runtime.execution.realtime_store import RealtimeWriterFactory
    engine=create_async_engine(os.environ['WORKMATE_TEST_POSTGRES_DSN'].replace(
        'postgresql://','postgresql+asyncpg://'),
        connect_args={'server_settings':{'search_path':sys.argv[1]}})
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    redis=Redis.from_url(os.environ['WORKMATE_TEST_REDIS_URL'],decode_responses=True,
        retry=Retry(NoBackoff(),0),retry_on_timeout=False)
    policy=RealtimePolicy()
    log=create_realtime_log(redis,namespace=os.environ['WORKMATE_TEST_REALTIME_NAMESPACE'],policy=policy)
    controller=RunningExecutionController(pool,verify_cleanup=quiescent,lease_seconds=2)
    controller.realtime_writer_factory=RealtimeWriterFactory(sessions,
        log_manager=EventLogManager(sessions,log),log=log,policy=policy)
    tool = ConfirmTool()
    tool.safety_ask = tool.is_concurrency_safe = True
    entered = asyncio.Event()
    mode = sys.argv[2]
    start, _ = prepare_reply([] if mode == 'running_stop' else [[TextBlock(text='before'),
        ToolCallBlock(id='a',name=tool.name,input='{}'),ToolCallBlock(id='b',name=tool.name,input='{}')]],
        tool=tool,entered=entered if mode == 'running_stop' else None)
    async def prepare_start(*args):
        prepared = await start(*args)
        stream_deltas(prepared.engine)
        return prepared
    async def prepare(owner,snapshot,guard):
        pending = snapshot.to_state().get_awaiting_tool_calls('test')
        return stream_deltas(make_engine([] if len(pending)>1 else [[TextBlock(text='after')]],
            state=snapshot.to_state(),tool=tool,middlewares=[ProductPermissionMiddleware(
                ExecutionPermissionPolicy(task_id=owner.task_id,approval_profile='standard')),guard]))
    async def allow(*args):
        return True  # Fixture only; no governed capability, RAG or external resources.
    shutdown = asyncio.Event()
    worker = WorkerService(pool,'node',WorkerBindings(prepare_start,prepare,prepare,allow,quiescent),
        controller=controller,
        capacity=1,lease_seconds=2,drain_seconds=0)
    async def input_shutdown():
        await asyncio.to_thread(sys.stdin.readline)
        shutdown.set()
    async def model_marker():
        await entered.wait()
        print('model_waiting',flush=True)
    reader = asyncio.create_task(input_shutdown())
    marker = asyncio.create_task(model_marker())
    try:
        print('ready',flush=True)
        await worker.serve(shutdown)
        print(json.dumps({'tool_calls':tool.calls,'failures':worker.failures}),flush=True)
    finally:
        reader.cancel()
        marker.cancel()
        await asyncio.gather(reader,marker,return_exceptions=True)
        await redis.aclose()
        await engine.dispose()
        await pool.close()


if __name__ == '__main__':
    asyncio.run(main())
