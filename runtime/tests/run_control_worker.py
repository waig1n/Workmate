"""Test-only single Worker process for Product admission/real Core integration."""
import asyncio
import json
import os
import sys
from dataclasses import asdict

import asyncpg
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from test_core_engine import ConfirmTool, make_engine
from test_execution_controller import prepare_reply
from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_runtime.execution.realtime_store import RealtimeWriterFactory
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.realtime.redis_log import RedisConversationLog
from workmate_product.realtime.log_manager import EventLogManager
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from workmate_runtime.execution.stopping import ParkedStopController
from workmate_runtime.execution.supervisor import ParkedSupervisor
from workmate_runtime.permission_policy import ExecutionPermissionPolicy, ProductPermissionMiddleware


def stream_deltas(engine):
    # ScriptModel normally emits only the final aggregate. This cross-role test
    # also exercises the delta events that real streaming adapters emit first.
    original = engine.agent.model._call_api
    async def call(*args, **kwargs):
        async def stream():
            async for chunk in await original(*args, **kwargs):
                yield ChatResponse(content=chunk.content,is_last=False)
                yield chunk
        return stream()
    engine.agent.model._call_api = call
    return engine


async def main():
    pool = await asyncpg.create_pool(os.environ['WORKMATE_TEST_POSTGRES_DSN'],
        server_settings={'search_path':sys.argv[1]})
    tool = ConfirmTool()
    tool.safety_ask = True
    tool.is_concurrency_safe = True
    database = create_async_engine(os.environ['WORKMATE_TEST_POSTGRES_DSN'].replace('postgresql://','postgresql+asyncpg://'),
        connect_args={'server_settings':{'search_path':sys.argv[1]}})
    sessions = async_sessionmaker(database, expire_on_commit=False)
    client = Redis.from_url(os.environ['WORKMATE_TEST_REDIS_URL'], decode_responses=True,
        retry=Retry(NoBackoff(),0))
    log = RedisConversationLog(client, namespace=os.environ['WORKMATE_TEST_REALTIME_NAMESPACE'])
    writer = RealtimeWriterFactory(sessions, log_manager=EventLogManager(sessions,log),
        log=log, policy=RealtimePolicy())
    resources = TaskResourceRegistry()
    controller = RunningExecutionController(pool, verify_cleanup=resources.verify_cleanup,
        realtime_writer_factory=writer)
    async def allow(db, actor, agent, task):
        return True  # This fixture has no governed capability or RAG dependency.
    async def prepare(owner, snapshot, guard):
        pending = snapshot.to_state().get_awaiting_tool_calls('test')
        responses = [] if len(pending)>1 else [[TextBlock(text='after')]]
        return stream_deltas(make_engine(responses, state=snapshot.to_state(),tool=tool,middlewares=[
            ProductPermissionMiddleware(ExecutionPermissionPolicy(task_id=owner.task_id,approval_profile='standard')),guard]))
    try:
        print('ready',flush=True)
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = line.strip()
            if command == 'start':
                start, _ = prepare_reply([[TextBlock(text='before'),
                    ToolCallBlock(id='a',name=tool.name,input='{}'),ToolCallBlock(id='b',name=tool.name,input='{}')]],tool=tool)
                async def streamed_start(owner, snapshot, guard):
                    resources.open_scope(owner, fresh_claim=True, all_resource_paths_tracked=True)
                    prepared = await start(owner, snapshot, guard)
                    stream_deltas(prepared.engine)
                    return prepared
                result = await controller.start(command,'worker','node',streamed_start)
            elif command == 'supervise':
                print('supervising',flush=True)
                result = await ParkedSupervisor(pool,lease_seconds=2,execution_controller=controller).serve(result.owner,'node',
                    resume_slots=asyncio.Semaphore(1),prepare_resume=prepare,prepare_stop=prepare,
                    authorize_resume=allow,verify_cleanup=resources.verify_cleanup)
            elif command == 'stop':
                result = await ParkedStopController(pool,realtime_writer_factory=writer).stop(command,'worker','node',prepare,verify_cleanup=resources.verify_cleanup)
            else:
                result = await controller.resume(command,'worker','node',prepare,authorize_resume=allow)
            print(json.dumps({'result':asdict(result) if result else None,'tool_calls':tool.calls}),flush=True)
    finally:
        await client.aclose()
        await database.dispose()
        await pool.close()


if __name__ == '__main__':
    asyncio.run(main())
