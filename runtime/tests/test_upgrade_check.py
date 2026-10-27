"""Read-only upgrade observations use random PostgreSQL schemas, never deployment state."""

import json

import pytest

from test_execution_context import database
from workmate_runtime.operations.upgrade_check import inspect_upgrade


async def revision(db):
    await db.execute("CREATE TABLE alembic_version(version_num text PRIMARY KEY)")
    await db.execute("INSERT INTO alembic_version VALUES ('0001_initial_schema')")


async def idle(db):
    await revision(db)
    await db.execute("UPDATE runtime_commands SET status='rejected'")
    await db.execute("UPDATE conversations SET active_product_task_id=NULL")
    await db.execute("UPDATE tasks SET status='cancelled'")


async def test_idle_report_is_only_a_candidate_and_never_exposes_content(database):
    _, db = database
    await idle(db)
    before = await db.fetchval("SELECT row_to_json(t)::text FROM tasks t")
    result = await inspect_upgrade(db, storage_node="node")
    assert result["drain_state"] == "candidate_requires_external_verification", result
    assert result["upgrade_authorized"] is False
    assert result["schema_revisions"] == ["0001_initial_schema"] and result["observed_at"]
    assert result["categories"] == {"idle": 1}
    assert result["snapshot_readability"] == "not_applicable"
    assert not any(
        secret in json.dumps(result)
        for secret in ("Be helpful", "forged", "password_hash")
    )
    assert before == await db.fetchval("SELECT row_to_json(t)::text FROM tasks t")


async def test_admitted_work_blocks_and_limit_is_not_a_green_light(database):
    _, db = database
    await revision(db)
    result = await inspect_upgrade(db, storage_node="node")
    assert result["drain_state"] == "blocked", result
    assert result["counts"]["commands_unsettled"] == 1
    await db.execute(
        "INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest) VALUES ('extra','s','c','t','u','StopTask','{}',repeat('a',64))"
    )
    result = await inspect_upgrade(db, storage_node="node", limit=1)
    assert result["drain_state"] == "unknown" and result["truncated"]
    assert "inspection_limit_exceeded" in result["issues"]


async def test_missing_schema_and_failed_queries_never_report_candidate(database):
    _, db = database
    result = await inspect_upgrade(db, storage_node="node")
    assert (
        result["drain_state"] == "unknown"
        and "missing_schema:alembic_version" in result["issues"]
    )
    await revision(db)
    await db.execute(
        "ALTER TABLE workspace_residencies RENAME COLUMN storage_node_id TO missing_column"
    )
    result = await inspect_upgrade(db, storage_node="node")
    assert result["drain_state"] == "unknown"
    assert result["issues"] == ["inspection_failed:UndefinedColumnError"]


async def test_inspection_transaction_enforces_read_only(database, monkeypatch):
    import workmate_runtime.operations.upgrade_check as checker

    _, db = database
    await idle(db)

    async def accidental_write(connection, **kwargs):
        assert (
            await connection.fetchval("SHOW transaction_isolation") == "repeatable read"
        )
        assert await connection.fetchval("SHOW transaction_read_only") == "on"
        await connection.execute("UPDATE tasks SET title='unexpected-write'")

    monkeypatch.setattr(checker, "inspect_sessions", accidental_write)
    result = await inspect_upgrade(db, storage_node="node")
    assert result["drain_state"] == "unknown"
    assert result["issues"] == ["inspection_failed:ReadOnlySQLTransactionError"]
    assert await db.fetchval("SELECT title FROM tasks WHERE id='t'") == "Task"


from test_realtime_redis_log import log  # noqa: E402,F401


async def released(database, log, *, with_body=True):
    from agentscope.message import TextBlock, ToolCallBlock
    from test_core_engine import ConfirmTool
    from test_execution_controller import prepare_reply
    from test_parked_stop import quiescent
    from test_suspension_store import receipt
    from workmate_runtime.execution.running import RunningExecutionController
    from workmate_runtime.execution.realtime_store import RealtimeWriterFactory
    from workmate_runtime.transport.product_pool import ProductWorkerPool
    from workmate_runtime.transport.suspension import SuspensionStore
    from workmate_product.config.realtime import RealtimePolicy
    from workmate_product.realtime.log_manager import EventLogManager

    sessions, db = database
    await revision(db)
    tool = ConfirmTool()
    tool.safety_ask = True
    tool.is_concurrency_safe = True
    blocks = [TextBlock(text="private response content")] if with_body else []
    blocks.append(ToolCallBlock(id="approval", name=tool.name, input="{}"))
    original, _ = prepare_reply([blocks], tool=tool)

    async def prepare(*args):
        from run_control_worker import stream_deltas

        prepared = await original(*args)
        stream_deltas(prepared.engine)
        return prepared

    writer = RealtimeWriterFactory(
        sessions,
        log_manager=EventLogManager(sessions, log),
        log=log,
        policy=RealtimePolicy(),
    )
    result = await RunningExecutionController(
        ProductWorkerPool(sessions.kw["bind"]),
        verify_cleanup=quiescent,
        realtime_writer_factory=writer,
    ).start("cmd", "worker", "node", prepare)
    await db.execute(
        "UPDATE tasks SET status='awaiting_permission',run_id=$1 WHERE id='t'",
        result.reply_id,
    )
    snapshot_id = await db.fetchval(
        "SELECT snapshot_id FROM runtime_sessions WHERE id='s'"
    )
    claim = await SuspensionStore(db).begin(
        result.owner, "node", snapshot_id, "test-generation"
    )
    assert claim is not None
    await SuspensionStore(db).release(claim, receipt(claim))
    # This fixture isolates upgrade evidence from Product projection behavior.
    await db.execute(
        "INSERT INTO runtime_projection_cursors(conversation_id,session_id,last_runtime_seq) SELECT conversation_id,id,runtime_seq FROM runtime_sessions"
    )
    return result


@pytest.mark.parametrize("with_body", [False, True])
async def test_exact_released_permission_is_readable_without_dumping_core(
    database, log, with_body
):
    await released(database, log, with_body=with_body)
    _, db = database
    result = await inspect_upgrade(db, storage_node="node")
    assert result["drain_state"] == "candidate_requires_external_verification", result
    assert result["categories"] == {"released_permission": 1}
    assert result["snapshot_counts"] == {"current_reader_readable": 1}
    assert result["upgrade_authorized"] is False
    assert "tool_calls" not in json.dumps(result)
    assert "private response content" not in json.dumps(result)


async def test_released_approval_with_missing_display_evidence_is_blocked(
    database, log
):
    await released(database, log)
    _, db = database
    # Corruption is injected only into this disposable test schema.
    await db.execute("ALTER TABLE runtime_events DISABLE TRIGGER USER")
    await db.execute(
        "UPDATE runtime_events SET payload=payload-'display_empty'-'display_snapshot' WHERE payload->>'type'='EXECUTION_BOUNDARY'"
    )
    result = await inspect_upgrade(db, storage_node="node")
    assert result["drain_state"] == "blocked", result
    assert result["snapshot_readability"] == "blocked"


async def test_unknown_core_version_is_not_upgrade_eligibility(database, log):
    await released(database, log)
    _, db = database
    await db.execute("ALTER TABLE runtime_snapshots DISABLE TRIGGER USER")
    await db.execute(
        "UPDATE runtime_snapshots SET payload=jsonb_set(payload,'{engine_version}','\"unknown-version\"')"
    )
    result = await inspect_upgrade(db, storage_node="node")
    assert result["drain_state"] == "blocked", result
    assert result["snapshot_readability"] == "blocked"


def test_cli_redacts_connection_errors(monkeypatch, capsys):
    import workmate_runtime.operations.upgrade_check as checker

    monkeypatch.setattr("sys.argv", ["upgrade_check", "--storage-node-id", "node"])

    async def fail(args):
        raise RuntimeError("postgresql://user:private-password@host/db private-prompt")

    monkeypatch.setattr(checker, "_main", fail)
    with pytest.raises(SystemExit) as stopped:
        checker.main()
    assert stopped.value.code == 2
    output = capsys.readouterr()
    assert not output.err and "private-" not in output.out
    assert json.loads(output.out)["upgrade_authorized"] is False


async def test_report_blocks_unprojected_events_even_with_released_readable_core(
    database, log
):
    await released(database, log)
    _, db = database
    await db.execute("UPDATE runtime_projection_cursors SET last_runtime_seq=0")
    report = await inspect_upgrade(db, storage_node="node")
    assert report["snapshot_readability"] == "current_reader_readable"
    assert report["counts"]["runtime_unprojected"] > 0
    assert report["drain_state"] == "blocked"


async def test_unknown_storage_node_cannot_hide_another_nodes_work(database):
    _, db = database
    await revision(db)
    report = await inspect_upgrade(db, storage_node="typo-node")
    assert report["drain_state"] == "unknown"
    assert report["issues"] == ["no_sessions_on_requested_node"]


async def test_oversized_display_boundary_is_not_loaded_or_accepted(database, log):
    await released(database, log)
    _, db = database
    await db.execute("ALTER TABLE runtime_events DISABLE TRIGGER USER")
    await db.execute(
        "UPDATE runtime_events SET payload=payload||jsonb_build_object('oversized',repeat('x',1048577)) WHERE payload->>'type'='EXECUTION_BOUNDARY'"
    )
    report = await inspect_upgrade(db, storage_node="node")
    assert report["drain_state"] == "blocked"
    assert report["snapshot_counts"] == {"unknown_display_boundary": 1}


@pytest.mark.parametrize("candidate", [False, True])
async def test_real_module_cli_uses_only_owned_schema(database, tmp_path, candidate):
    import asyncio
    import os
    import sys
    from urllib.parse import parse_qsl, urlsplit, urlunsplit, urlencode

    _, db = database
    if candidate:
        await idle(db)
    else:
        await revision(db)
    target = urlsplit(os.environ["WORKMATE_TEST_POSTGRES_DSN"])
    query = dict(parse_qsl(target.query))
    query["search_path"] = await db.fetchval("SELECT current_schema()")
    url = urlunsplit(
        (target.scheme, target.netloc, target.path, urlencode(query), target.fragment)
    )
    environment = {**os.environ, "DATABASE_URL": url}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "workmate_runtime.operations.upgrade_check",
        "--storage-node-id",
        "node",
        "--limit",
        "20",
        cwd=tmp_path,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        assert url.encode() not in stdout + stderr
        if target.password:
            assert target.password.encode() not in stdout + stderr
        assert stderr == b""
        report = json.loads(stdout)
        assert report["upgrade_authorized"] is False
        assert report["schema_revisions"] == ["0001_initial_schema"]
        assert report["drain_state"] == (
            "candidate_requires_external_verification" if candidate else "blocked"
        )
        assert process.returncode == (0 if candidate else 2)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_current_admission_ignores_historical_node_but_keeps_old_commands(database):
    from workmate_runtime.operations.upgrade_check import _COUNTS
    _, db = database
    await db.execute("""UPDATE runtime_sessions SET superseded_at=clock_timestamp() WHERE id='s';
        INSERT INTO runtime_sessions(id,conversation_id,storage_node_id) VALUES ('current','c','new-node');
        UPDATE tasks SET status='created';""")
    assert await db.fetchval(_COUNTS['product_work_pending'], 'node') == 0
    assert await db.fetchval(_COUNTS['product_work_pending'], 'new-node') == 1
    assert await db.fetchval(_COUNTS['commands_unsettled'], 'node') == 1
