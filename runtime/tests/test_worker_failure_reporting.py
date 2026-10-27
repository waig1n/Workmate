"""Worker composition reports failure only for its existing fenced execution."""
import json

import pytest

from test_execution_context import database, claim
from test_worker_workspaces import StubWorkspace
from workmate_runtime.config import RuntimeSettings
from workmate_runtime.execution.composition import compose_worker
from workmate_product.application.execution_quarantine import QuarantineConflict


def application(sessions, tmp_path):
    return compose_worker(sessions.kw['bind'], RuntimeSettings(reme_enabled=False),
        storage_node_id='node', workspace_dir=tmp_path/'workspaces', artifact_dir=tmp_path/'artifacts',
        max_file_bytes=1024, max_files=3, workspace_factory=StubWorkspace)


def failure(**changes):
    return dict(command_id='cmd', session_id='s', task_id='t', worker_id='worker',
                storage_node_id='node', error_type='RuntimeError', cause_type='ValueError') | changes


async def test_composed_callback_persists_failure_without_modifying_runtime_or_gates(database, tmp_path):
    sessions, admin = database
    await claim(admin)
    app = application(sessions, tmp_path)
    try:
        original = dict(await admin.fetchrow('SELECT * FROM runtime_commands'))
        report = app.service.bindings.report_failure
        assert await report(**failure()) is True
        assert await report(**failure()) is False
        assert await admin.fetchval('SELECT status FROM tasks') == 'failed'
        error = json.loads(await admin.fetchval('SELECT error FROM tasks'))
        assert error['code'] == 'runtime_execution_quarantined'
        assert error['context']['execution']['worker_id'] == 'worker'
        assert error['context']['execution']['execution_epoch'] == 1
        assert dict(await admin.fetchrow('SELECT * FROM runtime_commands')) == original
        assert await admin.fetchval('SELECT active_product_task_id FROM conversations') == 't'
        assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') == 't'
        assert await admin.fetchval('SELECT status FROM runtime_sessions') == 'preparing'
        assert await admin.fetchval('SELECT count(*) FROM product_events') == 1
    finally:
        await app.workspaces.close()


@pytest.mark.parametrize('changed', ['worker_id', 'storage_node_id', 'session_id', 'task_id', 'command_id'])
async def test_composed_callback_cannot_substitute_a_different_binding(database, tmp_path, changed):
    sessions, admin = database
    await claim(admin)
    app = application(sessions, tmp_path)
    try:
        with pytest.raises(QuarantineConflict):
            await app.service.bindings.report_failure(**failure(**{changed: 'different'}))
        assert await admin.fetchval('SELECT status FROM tasks') == 'created'
        assert await admin.fetchval('SELECT count(*) FROM product_events') == 0
        assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') == 't'
    finally:
        await app.workspaces.close()


async def test_composed_callback_rejects_execution_epoch_disagreement(database, tmp_path):
    sessions, admin = database
    await claim(admin)
    await admin.execute('UPDATE runtime_sessions SET execution_epoch=2')
    app = application(sessions, tmp_path)
    try:
        with pytest.raises(QuarantineConflict):
            await app.service.bindings.report_failure(**failure())
        assert await admin.fetchval('SELECT status FROM tasks') == 'created'
        assert await admin.fetchval('SELECT count(*) FROM product_events') == 0
    finally:
        await app.workspaces.close()
