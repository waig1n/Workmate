"""Recovery remains before delivery baseline and never borrows the old memory vault."""
from dataclasses import replace
from types import SimpleNamespace
import pytest
from test_worker_agent_assembly import assembly, OWNER, LocalGuard
from workmate_runtime.assembly.workspace_recovery import WorkspaceRecoveryError


async def test_recovery_precedes_delivery_and_disables_memory(assembly, monkeypatch):
    assembler, fixture, workspace = assembly
    ctx=fixture['context']
    fixture['context']=replace(ctx, initial_spec=ctx.initial_spec.model_copy(update={
        'recovery_fork_id':'fork','recovery_manifest_sha256':'a'*64}))
    calls=[]
    async def restore(context,binding,*args,**kwargs):
        assert context.initial_spec.recovery_fork_id=='fork'
        calls.append('restore')
    class Delivery:
        async def prepare(self,*args): calls.append('baseline')
    class Memory:
        chat_model=object()
        def middleware(self,*args,**kwargs): pytest.fail('Recovery must not borrow old memory')
    assembler.delivery_coordinator=Delivery()
    assembler.settings.reme_enabled=True
    assembler.memory_manager=Memory()
    monkeypatch.setattr('workmate_runtime.assembly.workspace_recovery.restore_recovery_workspace',restore)
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    assert calls==['restore','baseline']
    events=[event async for event in prepared.engine.reply_stream(prepared.inputs)]
    assert events[-1].type=='REPLY_END'


async def test_missing_recovery_file_prevents_prepared_agent(assembly, monkeypatch):
    assembler,fixture,workspace=assembly
    async def restore(*args,**kwargs): raise WorkspaceRecoveryError('missing object')
    class Delivery:
        async def prepare(self,*args): pytest.fail('baseline cannot start with missing recovery data')
    assembler.delivery_coordinator=Delivery()
    monkeypatch.setattr('workmate_runtime.assembly.workspace_recovery.restore_recovery_workspace',restore)
    with pytest.raises(WorkspaceRecoveryError,match='missing object'):
        await assembler.prepare_start(OWNER,None,LocalGuard())
    assert OWNER not in assembler._bound


async def test_recovery_context_is_internal_and_does_not_mutate_display_input(assembly, monkeypatch):
    assembler,fixture,workspace=assembly
    ctx=fixture['context']
    fixture['context']=replace(ctx,initial_spec=ctx.initial_spec.model_copy(update={
        'recovery_fork_id':'fork','recovery_manifest_sha256':'a'*64,
        'recovery_context_text':'Earlier conversation and file inventory\nOriginal request'}))
    async def restore(*args,**kwargs): pass
    monkeypatch.setattr('workmate_runtime.assembly.workspace_recovery.restore_recovery_workspace',restore)
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    assert prepared.inputs[0].get_text_content()=='Earlier conversation and file inventory\nOriginal request'
    assert prepared.inputs[0].id==ctx.input_messages[0].id
    assert fixture['context'].input_messages[0].content=={'text':'hello'}
