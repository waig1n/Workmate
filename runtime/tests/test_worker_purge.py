"""Runtime workspace purge after restart, with Product persistence fixtures."""
from datetime import datetime, timezone
from pathlib import Path

from workmate_product.persistence.models import Conversation, ConversationRetirement
from test_conversation_purge import prepare
from test_durable_projection import projection_database  # noqa: F401


async def test_worker_purges_after_restart_and_empty_conversation(projection_database, tmp_path):
    from types import SimpleNamespace
    from workmate_runtime.execution.retirement import RetirementWorker
    from test_worker_workspaces import manager
    from workmate_runtime.assembly.workspaces import WorkspaceIdentity
    sessions, admin = projection_database
    await prepare(sessions, admin, tmp_path/'artifacts')
    identity = WorkspaceIdentity('u','a','c','s','node')
    old = manager(tmp_path/'workspace')
    ws = await old.get_workspace(identity)
    path = Path(ws.host_workdir)/'output/secret.txt'
    path.write_text('private')
    await old.close()
    restarted = manager(tmp_path/'workspace')
    async with sessions.begin() as db:
        db.add(Conversation(id='empty',user_id='u',agent_id='a',deleted_at=datetime.now(timezone.utc)))
        await db.flush()
        db.add(ConversationRetirement(conversation_id='empty',requested_by='u',execution_epoch=0,
            completed_at=datetime.now(timezone.utc),completed_by='product'))
    worker = RetirementWorker(sessions, SimpleNamespace(node_id='node',worker_id='new',active_sessions=set()),
        restarted, artifact_dir=tmp_path/'artifacts')
    try:
        await worker.purge_pending()
        assert worker.failures == {}
        assert not path.exists()
        assert await admin.fetchval('SELECT count(*) FROM conversation_retirements WHERE purged_at IS NOT NULL') == 2
    finally:
        await restarted.close()
