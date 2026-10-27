"""Only trusted lineage may pin recovery execution and disable local memory."""
from types import SimpleNamespace
import pytest
from workmate_product.application.recovery_context import recovery_spec_fields
from workmate_contracts import snapshot_digest

class Database:
    def __init__(self,row): self.row=row
    async def scalar(self,query): return self.row

@pytest.mark.asyncio
async def test_normal_spec_retains_normal_memory():
    assert await recovery_spec_fields(Database(None),SimpleNamespace(id='c',user_id='u'),SimpleNamespace(id='s',storage_node_id='n',superseded_at=None)) == {}

@pytest.mark.asyncio
async def test_frozen_recovery_pins_manifest_and_target():
    manifest={'version':1,'files':[],'directories':[],'total_bytes':0}
    row=SimpleNamespace(id='fork',conversation_id='c',actor_user_id='u',target_node_id='n',manifest=manifest,target_task_id='t',context_text='context')
    assert await recovery_spec_fields(Database(row),SimpleNamespace(id='c',user_id='u'),SimpleNamespace(id='s',storage_node_id='n',superseded_at=None)) == {
        'recovery_fork_id':'fork','recovery_manifest_sha256':snapshot_digest(manifest),'recovery_context_text':None}

@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('actor_user_id','other'),('conversation_id','other'),('target_node_id','other')])
async def test_mismatched_recovery_rejected(field,value):
    from workmate_product.application.execution_context import ExecutionContextRejected
    row=SimpleNamespace(id='fork',conversation_id='c',actor_user_id='u',target_node_id='n',manifest={},target_task_id='t',context_text='context')
    setattr(row,field,value)
    with pytest.raises(ExecutionContextRejected):
        await recovery_spec_fields(Database(row),SimpleNamespace(id='c',user_id='u'),SimpleNamespace(id='s',storage_node_id='n',superseded_at=None))


@pytest.mark.asyncio
async def test_first_recovery_task_freezes_context_only_once():
    row=SimpleNamespace(id='fork',conversation_id='c',actor_user_id='u',target_node_id='n',manifest={},target_task_id='t',context_text='context')
    result=await recovery_spec_fields(Database(row),SimpleNamespace(id='c',user_id='u'),SimpleNamespace(id='s',storage_node_id='n',superseded_at=None),task_id='t')
    assert result['recovery_context_text']=='context'
