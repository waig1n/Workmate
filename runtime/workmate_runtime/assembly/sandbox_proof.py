"""Persist provider-verified Task isolation under current execution authority."""
from sqlalchemy import text, bindparam, JSON
from workmate_runtime.execution.controller import ExecutionAborted, SandboxVerificationFailed


async def attest_task_sandbox(binding,owner,sessions):
    if binding.owner != owner:
        raise SandboxVerificationFailed('Only a bound production Task backend can attest sandbox isolation')
    if getattr(binding.backend,"_execution_owner",None) != owner:
        raise SandboxVerificationFailed("Task backend owner differs from the proof owner")
    local = getattr(binding.provider, "name", None) == "local"
    if local:
        from workmate_runtime.sandbox.local import LocalSandboxProvider, LocalTaskBackend
        from workmate_runtime.execution.resources import TaskResourceRegistry
        if (type(binding.provider) is not LocalSandboxProvider
                or type(binding.backend) is not LocalTaskBackend
                or not isinstance(getattr(binding.backend, "_task_resources", None), TaskResourceRegistry)):
            raise SandboxVerificationFailed("Local execution requires the trusted configured provider")
        binding.backend._task_resources._scope(owner, launching=True)
        # An explicitly unisolated provider has no isolation proof to persist.
        proof = None
    else:
        proof = await binding.provider.verify_task_isolation(binding.backend)
        if proof is None and binding.provider.name == "bubblewrap":
            proof = {"isolation":"bubblewrap","containment":"pid_namespace","instance_id":"","evidence":{}}
        from workmate_product.application.sandbox_proof import sandbox_proof_valid
        if not sandbox_proof_valid(proof):
            raise SandboxVerificationFailed("Sandbox provider did not produce valid instance evidence")
    async with sessions.begin() as db:
        # Order Task -> Session as all execution writes; require preparing so a
        # late/cancelled probe never retroactively certifies a running Task.
        task=(await db.execute(text('SELECT status,approval_profile FROM tasks WHERE id=:id FOR UPDATE'),
            {'id':owner.task_id})).mappings().first()
        session=(await db.execute(text('''SELECT * FROM runtime_sessions WHERE id=:id AND superseded_at IS NULL FOR UPDATE'''),
            {'id':owner.session_id})).mappings().first()
        fresh = (task is not None and task['status']=='created' and session is not None
                 and session['status']=='preparing' and session['reply_id'] is None)
        cold = False
        if session is not None and not fresh:
            from workmate_runtime.transport.product_pool import ProductConnection
            from workmate_runtime.transport.suspension import read_recovery_bridge
            connection = await db.connection()
            raw = await connection.get_raw_connection()
            cold = await read_recovery_bridge(ProductConnection(connection,raw.driver_connection),owner) is not None
        if (not task or not session or not (fresh or cold) or session['active_task_id']!=owner.task_id
                or session['owner_id']!=owner.worker_id or session['execution_epoch']!=owner.execution_epoch
                or not await db.scalar(text('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=:id AND superseded_at IS NULL'),
                    {'id':owner.session_id})):
            raise ExecutionAborted('Task authority changed during sandbox verification')
        if local:
            if task['approval_profile'] == 'full_access':
                raise SandboxVerificationFailed('Full Access requires an isolated sandbox; local execution is unisolated')
            return
        await db.execute(text('''INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,
            execution_epoch,workspace_id,isolation,containment,instance_id,evidence)
            VALUES (:task,:session,:worker,:epoch,:workspace,:isolation,:containment,:instance_id,:evidence)''').bindparams(bindparam("evidence",type_=JSON)),
            {'task':owner.task_id,'session':owner.session_id,'worker':owner.worker_id,
             'epoch':owner.execution_epoch,'workspace':binding.workspace.workspace_id,**proof})
