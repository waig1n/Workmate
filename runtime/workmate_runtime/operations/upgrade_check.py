"""Bounded point-in-time upgrade observations, not upgrade authorization.

DATABASE_URL supplies the connection. Admission/claim freezing is external;
this command neither drains Workers nor opens Workspaces or invokes tools.
"""

import argparse
import asyncio
from collections import Counter
import json
import os
import re
from types import SimpleNamespace

from workmate_runtime.execution.reconciliation import inspect_sessions
from workmate_runtime.transport.suspension import _released_snapshot_matches
from workmate_contracts.realtime import MessageSnapshotReference, message_id
from workmate_product.persistence.repositories.message_snapshots import from_message

_MAX_SNAPSHOT_BYTES = 1024 * 1024
_REQUIRED = (
    "alembic_version",
    "runtime_sessions",
    "conversations",
    "tasks",
    "runtime_task_executions",
    "runtime_snapshots",
    "runtime_suspensions",
    "runtime_commands",
    "runtime_events",
    "runtime_projection_cursors",
    "product_events",
    "realtime_business_outbox",
    "command_deliveries",
    "runtime_delivery_captures",
    "workspace_residencies",
    "task_messages",
)
_COUNTS = {
    "product_work_pending": """SELECT count(*) FROM tasks t
        JOIN runtime_sessions s ON s.conversation_id=t.conversation_id AND s.superseded_at IS NULL
        WHERE s.storage_node_id=$1 AND t.status IN ('created','running','cancelling')""",
    "orphan_admissions_global": """SELECT count(*) FROM conversations c
        WHERE c.active_product_task_id IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM runtime_sessions s WHERE s.conversation_id=c.id AND s.superseded_at IS NULL)
        AND $1::text IS NOT NULL""",
    "runtime_unprojected": """SELECT count(*) FROM runtime_events e
        JOIN runtime_sessions s ON s.id=e.session_id
        LEFT JOIN runtime_projection_cursors p ON p.session_id=s.id
        WHERE s.storage_node_id=$1 AND e.runtime_seq>coalesce(p.last_runtime_seq,0)""",
    "business_unpublished_or_missing_outbox": """SELECT count(DISTINCT e.id) FROM product_events e
        JOIN runtime_sessions s ON s.conversation_id=e.conversation_id
        LEFT JOIN realtime_business_outbox o ON o.event_id=e.id
        WHERE s.storage_node_id=$1 AND e.business_seq IS NOT NULL
        AND (o.event_id IS NULL OR o.status<>'published')""",
    "outbox_pending": """SELECT count(DISTINCT o.event_id) FROM realtime_business_outbox o
        JOIN runtime_sessions s ON s.conversation_id=o.conversation_id
        WHERE s.storage_node_id=$1 AND o.status<>'published'""",
    "commands_unsettled": """SELECT count(*) FROM runtime_commands c
        JOIN runtime_sessions s ON s.id=c.session_id WHERE s.storage_node_id=$1
        AND c.status NOT IN ('completed','rejected')""",
    "command_deliveries_unsettled": """SELECT count(*) FROM command_deliveries d
        JOIN runtime_commands c ON c.id=d.command_id JOIN runtime_sessions s ON s.id=c.session_id
        WHERE s.storage_node_id=$1 AND d.status NOT IN ('published','settled','superseded')""",
    "delivery_pending": """SELECT count(*) FROM runtime_sessions s
        WHERE s.storage_node_id=$1 AND s.active_task_id IS NOT NULL
        AND s.status IN ('completed','failed','cancelled','interrupted')
        AND NOT EXISTS(SELECT 1 FROM runtime_delivery_captures d WHERE d.task_id=s.active_task_id)""",
    "workspace_unreleased": """SELECT count(*) FROM workspace_residencies
        WHERE storage_node_id=$1 AND status<>'released'""",
}


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


async def _readable(db, observation):
    """Exact released source plus current reader/display evidence; no file I/O."""
    size = await db.fetchval(
        "SELECT octet_length(payload::text) FROM runtime_snapshots WHERE id=$1",
        observation.snapshot_id,
    )
    if size is None or size > _MAX_SNAPSHOT_BYTES:
        return "unknown_snapshot"
    snapshot = await db.fetchrow(
        "SELECT * FROM runtime_snapshots WHERE id=$1", observation.snapshot_id
    )
    binding = await db.fetchrow(
        """SELECT t.run_id,t.status,s.conversation_id FROM tasks t
        JOIN runtime_sessions s ON s.id=$2 WHERE t.id=$1""",
        snapshot["task_id"],
        snapshot["session_id"],
    )
    if (
        binding is None
        or binding["run_id"] != snapshot["reply_id"]
        or binding["status"] != "awaiting_permission"
    ):
        return "inconsistent_product_reply"
    pause = await db.fetchrow(
        "SELECT * FROM runtime_suspensions WHERE snapshot_id=$1",
        observation.snapshot_id,
    )
    if (
        pause is None
        or pause["resume_epoch"] is not None
        or not _released_snapshot_matches(pause, snapshot)
    ):
        return "unreadable_core_or_release"
    event_size = await db.fetchval(
        """SELECT octet_length(payload::text) FROM runtime_events
        WHERE session_id=$1 AND task_id=$2 AND execution_epoch=$3 AND runtime_seq=$4""",
        snapshot["session_id"],
        snapshot["task_id"],
        snapshot["execution_epoch"],
        snapshot["runtime_seq"],
    )
    if event_size is not None and event_size > _MAX_SNAPSHOT_BYTES:
        return "unknown_display_boundary"
    event = await db.fetchrow(
        """SELECT payload FROM runtime_events
        WHERE session_id=$1 AND task_id=$2 AND execution_epoch=$3 AND runtime_seq=$4""",
        snapshot["session_id"],
        snapshot["task_id"],
        snapshot["execution_epoch"],
        snapshot["runtime_seq"],
    )
    if event is None:
        return "missing_display_boundary"
    payload = _decode(event["payload"])
    if (
        payload.get("type") != "EXECUTION_BOUNDARY"
        or payload.get("status") != "awaiting_interaction"
        or payload.get("reply_id") != snapshot["reply_id"]
    ):
        return "invalid_display_boundary"
    mid = message_id(snapshot["task_id"], snapshot["reply_id"])
    size = await db.fetchval(
        "SELECT octet_length(content::text) FROM task_messages WHERE id=$1", mid
    )
    if size is not None and size > _MAX_SNAPSHOT_BYTES:
        return "unknown_display"
    row = await db.fetchrow("SELECT * FROM task_messages WHERE id=$1", mid)
    if payload.get("display_empty") is True:
        return (
            "current_reader_readable"
            if row is None and "display_snapshot" not in payload
            else "invalid_empty_display"
        )
    try:
        reference = MessageSnapshotReference.model_validate(
            payload.get("display_snapshot")
        )
        if (
            row is None
            or row["role"] != "assistant"
            or len(row["content"]) > _MAX_SNAPSHOT_BYTES
        ):
            return "unknown_display"
        values = dict(row)
        values["content"] = _decode(values["content"])
        display = from_message(SimpleNamespace(**values))
        valid = (
            display.message_id == message_id(snapshot["task_id"], snapshot["reply_id"])
            and display.conversation_id == binding["conversation_id"]
            and display.task_id == snapshot["task_id"]
            and display.reply_id == snapshot["reply_id"]
            and display.execution_epoch == snapshot["execution_epoch"]
            and display.phase == "approval"
            and not display.incomplete
            and MessageSnapshotReference.from_snapshot(display) == reference
        )
        return "current_reader_readable" if valid else "inconsistent_display"
    except (ValueError, TypeError, KeyError):
        return "unreadable_display"


async def inspect_upgrade(connection, *, storage_node, limit=100):
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", storage_node)
        or type(limit) is not int
        or not 1 <= limit <= 1000
    ):
        raise ValueError("Require a bounded storage-node identity and limit 1..1000")
    from workmate_runtime.engines.agentscope import CoreSnapshot

    report = {
        "storage_node": storage_node,
        "limit": limit,
        "observed_at": None,
        "schema_revisions": [],
        "reader": {
            "core_format_version": CoreSnapshot.model_fields["format_version"].default,
            "engine_version": CoreSnapshot.model_fields["engine_version"].default,
        },
        "drain_state": "unknown",
        "snapshot_readability": "unknown",
        "counts": {},
        "samples": [],
        "truncated": False,
        "issues": [],
        "upgrade_authorized": False,
        "limitations": [
            "point_in_time_only",
            "admission_and_claim_freeze_required_externally",
            "current_engine_reader_only_not_old_image_compatibility",
            "workspace_and_offload_files_not_verified",
            "redis_pending_entries_not_inspected",
            "global_inspection_limit_includes_other_storage_nodes",
        ],
    }
    try:
        async with asyncio.timeout(10):
            async with connection.transaction(
                isolation="repeatable_read", readonly=True
            ):
                await connection.execute("SET LOCAL statement_timeout='3000ms'")
                report["observed_at"] = (
                    await connection.fetchval("SELECT transaction_timestamp()")
                ).isoformat()
                missing = [
                    name
                    for name in _REQUIRED
                    if await connection.fetchval("SELECT to_regclass($1)", name) is None
                ]
                if missing:
                    report["issues"] = ["missing_schema:" + name for name in missing]
                    return report
                report["schema_revisions"] = [
                    r["version_num"]
                    for r in await connection.fetch(
                        "SELECT version_num FROM alembic_version"
                    )
                ]
                if not report["schema_revisions"]:
                    report["issues"].append("missing_schema_revision")
                    return report
                report["counts"] = {
                    name: await connection.fetchval(query, storage_node)
                    for name, query in _COUNTS.items()
                }
                # Existing reconciliation intentionally classifies all Sessions.
                # Bound that complete scan rather than silently omit a tail.
                total = await connection.fetchval(
                    "SELECT count(*) FROM runtime_sessions"
                )
                commands = await connection.fetchval(
                    "SELECT count(*) FROM runtime_commands WHERE status IN ('accepted','claimed','indeterminate')"
                )
                report["counts"]["sessions_global"] = total
                report["counts"]["sessions_on_node"] = await connection.fetchval(
                    "SELECT count(*) FROM runtime_sessions WHERE storage_node_id=$1",
                    storage_node,
                )
                if report["counts"]["sessions_on_node"] == 0:
                    report["issues"].append("no_sessions_on_requested_node")
                    return report
                if max(total, commands) > limit:
                    report["truncated"] = True
                    report["issues"].append("inspection_limit_exceeded")
                    return report
                ids = {
                    r["id"]
                    for r in await connection.fetch(
                        "SELECT id FROM runtime_sessions WHERE storage_node_id=$1",
                        storage_node,
                    )
                }
                observations = [
                    o
                    for o in await inspect_sessions(
                        connection,
                        worker_id="upgrade-read-only",
                        storage_node_id=storage_node,
                    )
                    if o.session_id in ids
                ]
                categories = Counter(o.category for o in observations)
                report["categories"] = dict(categories)
                readable = Counter()
                for observation in observations:
                    state = "not_applicable"
                    if observation.category == "released_permission":
                        state = await _readable(connection, observation)
                        readable[state] += 1
                    elif observation.snapshot_id is not None:
                        size = await connection.fetchval(
                            "SELECT octet_length(payload::text) FROM runtime_snapshots WHERE id=$1",
                            observation.snapshot_id,
                        )
                        state = "unknown_snapshot"
                        if size is not None and size <= _MAX_SNAPSHOT_BYTES:
                            from workmate_runtime.engines.agentscope import CoreSnapshot

                            payload = await connection.fetchval(
                                "SELECT payload FROM runtime_snapshots WHERE id=$1",
                                observation.snapshot_id,
                            )
                            try:
                                core = CoreSnapshot.model_validate(
                                    _decode(payload)
                                ).to_state()
                                state = (
                                    "current_reader_readable"
                                    if core.session_id == observation.session_id
                                    else "inconsistent_core"
                                )
                            except (ValueError, TypeError):
                                state = "unreadable_core"
                        readable[state] += 1
                    if len(report["samples"]) < min(limit, 20):
                        report["samples"].append(
                            {
                                "session_id": observation.session_id,
                                "task_id": observation.task_id,
                                "category": observation.category,
                                "snapshot_readability": state,
                            }
                        )
                report["snapshot_counts"] = dict(readable)
                report["snapshot_readability"] = (
                    "blocked"
                    if any(k != "current_reader_readable" for k in readable)
                    else "current_reader_readable"
                    if readable
                    else "not_applicable"
                )
                blocked = any(
                    o.category not in {"idle", "released_permission"}
                    for o in observations
                )
                report["drain_state"] = (
                    "blocked"
                    if blocked
                    or any(report["counts"][k] for k in _COUNTS)
                    or report["snapshot_readability"] == "blocked"
                    else "candidate_requires_external_verification"
                )
    except Exception as error:
        # DB/validation exceptions may embed credentials and private state.
        report["drain_state"] = report["snapshot_readability"] = "unknown"
        report["issues"].append("inspection_failed:" + type(error).__name__)
    return report


async def _main(args):
    import asyncpg

    url = os.environ.get("DATABASE_URL", "").replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    if not url:
        raise ValueError("DATABASE_URL is required")
    connection = await asyncpg.connect(url, timeout=5)
    try:
        return await inspect_upgrade(
            connection, storage_node=args.storage_node, limit=args.limit
        )
    finally:
        await connection.close(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage-node-id", "--storage-node", dest="storage_node", required=True
    )
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    try:
        result = asyncio.run(_main(args))
    except Exception as error:
        result = {
            "drain_state": "unknown",
            "upgrade_authorized": False,
            "issues": ["connection_or_config_failed:" + type(error).__name__],
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(
        0 if result["drain_state"] == "candidate_requires_external_verification" else 2
    )


if __name__ == "__main__":
    main()
