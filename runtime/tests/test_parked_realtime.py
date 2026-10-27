"""Real Core and PostgreSQL parked cleanup waits for the exact display cut."""
import json
from uuid import uuid4

import pytest
from workmate_contracts.realtime import MessageSnapshot, MessageSnapshotReference
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.realtime.message_accumulator import MessageAccumulator
from workmate_product.realtime.redis_log import LogRef
from workmate_runtime.execution.controller import ExecutionAborted
from workmate_runtime.execution.realtime_writer import RealtimeWriter
from workmate_runtime.execution.stopping import ParkedStopController
from test_core_engine import ConfirmTool
from test_parked_stop import stop_intent, quiescent
from test_postgres_execution_store import database  # noqa: F401
from test_resume_controller import parked, prepare_resume


class DisplayFactory:
    def __init__(self,pool,reply,*,body=True,fail=False):
        self.pool,self.reply,self.body,self.fail=pool,reply,body,fail
        self.closed=False
        self.accepted=[]
        self.initial=None
        self.cut=None
        self.ref=LogRef('conversation',str(uuid4()),str(uuid4()))

    async def __call__(self,guard,initial):
        assert guard.reply_id==self.reply
        assert initial.reply_id==self.reply
        self.initial=initial
        acc=MessageAccumulator('conversation','task',self.reply,guard.owner.execution_epoch)
        if self.body:
            acc.apply({'type':'TEXT_BLOCK_START','reply_id':self.reply,'block_id':'body'})
            acc.apply({'type':'TEXT_BLOCK_DELTA','reply_id':self.reply,'block_id':'body','delta':'审批前正文'})
            restored=acc.capture('approval',None,0)
            acc=MessageAccumulator('conversation','task',self.reply,guard.owner.execution_epoch,restored)
        self.writer=RealtimeWriter(acc,conversation_id='conversation',log_manager=self,log=self,
            check_authority=guard.display_authority,read_business_seq=self.business,
            save_snapshot=self.save,policy=RealtimePolicy())
        return self

    async def business(self): return 0
    async def ensure(self,cid): return self.ref
    async def is_current(self,ref): return ref==self.ref
    async def append(self,*args): raise AssertionError('Cleanup must not publish body deltas')

    async def accept(self,raw):
        self.accepted.append(raw)
        await self.writer.accept(raw)

    async def checkpoint(self,phase):
        # Production lazy factory has this same no-body contract.
        if not self.body: return None
        self.cut=await self.writer.checkpoint(phase)
        return self.cut

    async def save(self,snapshot,*,unanchored):
        if self.fail: raise RuntimeError('snapshot unavailable')
        async with self.pool.acquire() as db:
            await db.execute('INSERT INTO display_cuts(payload) VALUES ($1::jsonb)',snapshot.model_dump_json())

    async def close(self):
        await self.writer.close()
        self.closed=True


@pytest.mark.parametrize('body',[True,False])
async def test_parked_stop_saves_restored_display_before_sealing_without_ghost(database,body):
    pool,db=database
    first=await parked(pool,db)
    await db.execute('CREATE TABLE display_cuts(payload jsonb)')
    await stop_intent(db)
    factory=DisplayFactory(pool,first.reply_id,body=body)
    sealed=[]
    async def seal(*args):
        sealed.append(True)
        assert await db.fetchval('SELECT count(*) FROM display_cuts')==int(body)
        assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='claimed'
        return 'a'*64
    result=await ParkedStopController(pool,seal_terminal=seal,realtime_writer_factory=factory).stop(
        'stop','worker','node',prepare_resume(ConfirmTool()),verify_cleanup=quiescent)
    assert result.status=='interrupted' and sealed and factory.closed
    boundary=json.loads(await db.fetchval("SELECT payload FROM runtime_events WHERE payload->>'type'='EXECUTION_BOUNDARY' ORDER BY runtime_seq DESC LIMIT 1"))
    if body:
        saved=MessageSnapshot.model_validate_json(await db.fetchval('SELECT payload FROM display_cuts'))
        assert saved.phase=='interrupted' and saved.blocks[0].closed
        assert saved.blocks[0].text=='审批前正文' and saved.snapshot_version==2
        assert boundary['display_snapshot']==MessageSnapshotReference.from_snapshot(saved).model_dump(mode='json')
    else:
        assert 'display_snapshot' not in boundary
    types=[raw['type'] for raw in factory.accepted]
    assert 'TOOL_RESULT_END' in types and types[-1]=='REPLY_END'
    assert await db.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type'='TOOL_RESULT_END'")>0


async def test_display_save_failure_cannot_seal_or_finish_parked_stop(database):
    pool,db=database
    first=await parked(pool,db)
    await db.execute('CREATE TABLE display_cuts(payload jsonb)')
    await stop_intent(db)
    factory=DisplayFactory(pool,first.reply_id,fail=True)
    before=await db.fetchval('SELECT runtime_seq FROM runtime_sessions')
    async def seal(*args): raise AssertionError('Unsaved cut cannot seal')
    with pytest.raises(ExecutionAborted):
        await ParkedStopController(pool,seal_terminal=seal,realtime_writer_factory=factory).stop(
            'stop','worker','node',prepare_resume(ConfirmTool()),verify_cleanup=quiescent)
    assert factory.closed
    assert await db.fetchval('SELECT runtime_seq FROM runtime_sessions')==before
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='claimed'
    assert await db.fetchval('SELECT count(*) FROM display_cuts')==0


def test_invalid_parked_realtime_factory_fails_configuration():
    with pytest.raises(TypeError,match='factory'):
        ParkedStopController(None,realtime_writer_factory=object())


async def test_unproven_cleanup_closes_writer_without_snapshot_or_seal(database):
    pool,db=database
    first=await parked(pool,db)
    await db.execute('CREATE TABLE display_cuts(payload jsonb)')
    await stop_intent(db)
    factory=DisplayFactory(pool,first.reply_id)
    async def unproven(*args): return False
    async def seal(*args): raise AssertionError('Unproven cleanup cannot seal')
    with pytest.raises(ExecutionAborted):
        await ParkedStopController(pool,seal_terminal=seal,realtime_writer_factory=factory).stop(
            'stop','worker','node',prepare_resume(ConfirmTool()),verify_cleanup=unproven)
    assert factory.closed
    assert await db.fetchval('SELECT count(*) FROM display_cuts')==0
