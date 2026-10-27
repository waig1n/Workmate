"""Realtime faults settle only from durable Reply, cleanup and display evidence."""

import json

import pytest

from workmate_contracts.realtime import MessageSnapshotReference
from workmate_product.application.message_snapshots import save_message_snapshot
from workmate_product.projections.store import ProjectionConflict
from workmate_product.realtime.message_accumulator import MessageAccumulator
from test_durable_projection import projection_database  # noqa: F401
from test_durable_lifecycle import START, boundary, consume, stream


async def prepare(
    sessions, admin, *, reason, body, phase="interrupted", incomplete=True
):
    # Only display content is synthesized by its real accumulator. Core state
    # remains opaque to this Product test, as in the existing boundary fixture.
    await stream(
        sessions, admin, [START, {"type": "REPLY_END", "finished_reason": reason}]
    )
    evidence = {
        "execution_failure": {
            "code": "realtime_unavailable",
            "finished_reason": reason,
        },
        "resource_cleanup": {"core_drained": True, "resources_quiescent": True},
    }
    if not body:
        evidence["display_empty"] = True
        return evidence
    accumulator = MessageAccumulator("c", "t", "reply", 1)
    accumulator.apply({"type": "TEXT_BLOCK_START", "block_id": "b"})
    accumulator.apply(
        {"type": "TEXT_BLOCK_DELTA", "block_id": "b", "delta": "已保存的正文🙂"}
    )
    cut = accumulator.capture(phase, None, 0, incomplete=incomplete, unanchored=True)
    await admin.execute(
        "UPDATE runtime_sessions SET owner_id='owner',lease_until=clock_timestamp()+interval '60 seconds'"
    )
    async with sessions.begin() as db:
        await save_message_snapshot(db, cut, owner_id="owner", unanchored=True)
    evidence["display_snapshot"] = MessageSnapshotReference.from_snapshot(
        cut
    ).model_dump(mode="json")
    return evidence


@pytest.mark.parametrize("reason", ["interrupted", "completed"])
@pytest.mark.parametrize("body", [False, True])
async def test_verified_realtime_fault_is_failed_not_user_cancelled(
    projection_database, reason, body
):
    sessions, admin = projection_database
    evidence = await prepare(sessions, admin, reason=reason, body=body)
    seq = await boundary(admin, status="failed", extra=evidence)
    await consume(sessions)
    await consume(sessions)  # Idempotent replay must not emit another terminal.
    assert await admin.fetchval("SELECT status FROM tasks") == "failed"
    error = json.loads(await admin.fetchval("SELECT error FROM tasks"))
    assert error["type"] == "realtime_unavailable"
    assert error["runtimeReason"] == reason
    assert (
        error["message"]
        == "Realtime delivery became unavailable; the task was stopped safely."
    )
    assert set(error) == {"type", "message", "runtimeReason"}
    public = [
        json.loads(row["payload"])
        for row in await admin.fetch(
            "SELECT payload FROM product_events WHERE source_runtime_event_id=$1 ORDER BY product_seq",
            str(seq),
        )
    ]
    assert sum(item["type"] == "RUN_ERROR" for item in public) == 1
    assert not any(item["type"] in {"RUN_CANCELLED", "RUN_FINISHED"} for item in public)
    completed = next(item for item in public if item.get("name") == "TASK_COMPLETED")
    assert completed["value"]["taskStatus"] == "failed"
    assert completed["value"]["reason"] == reason
    assert completed["value"]["completionGate"] == "realtime_unavailable"
    key = "display_snapshot" if body else "display_empty"
    assert all(item.get(key) == evidence[key] for item in public)
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM task_events WHERE event_type='task_cancelled'"
        )
        == 0
    )
    if body:
        row = await admin.fetchrow(
            "SELECT content,snapshot_phase,snapshot_incomplete FROM task_messages WHERE role='assistant'"
        )
        assert json.loads(row["content"])["text"] == "已保存的正文🙂"
        assert (
            row["snapshot_phase"] == "interrupted"
            and row["snapshot_incomplete"] is True
        )
    else:
        assert await admin.fetchval("SELECT count(*) FROM task_messages") == 0


@pytest.mark.parametrize(
    "case",
    [
        "missing_failure",
        "unknown_code",
        "wrong_reason",
        "missing_reason",
        "missing_cleanup",
        "core_not_drained",
        "resources_not_quiescent",
        "integer_cleanup",
        "missing_display",
        "false_empty",
    ],
)
async def test_incomplete_or_mismatched_fault_evidence_rolls_back(
    projection_database, case
):
    sessions, admin = projection_database
    # Error already matches failed, so validation cannot merely rely on the
    # preexisting mismatch check used for interrupted/completed Reply endings.
    reason = "interrupted" if case == "missing_failure" else "error"
    evidence = await prepare(sessions, admin, reason=reason, body=False)
    if case == "missing_failure":
        del evidence["execution_failure"]
    elif case == "unknown_code":
        evidence["execution_failure"]["code"] = "arbitrary_failure"
    elif case == "wrong_reason":
        evidence["execution_failure"]["finished_reason"] = "completed"
    elif case == "missing_reason":
        del evidence["execution_failure"]["finished_reason"]
    elif case == "missing_cleanup":
        del evidence["resource_cleanup"]
    elif case == "core_not_drained":
        evidence["resource_cleanup"]["core_drained"] = False
    elif case == "resources_not_quiescent":
        evidence["resource_cleanup"]["resources_quiescent"] = False
    elif case == "integer_cleanup":
        evidence["resource_cleanup"]["core_drained"] = 1
    elif case == "missing_display":
        del evidence["display_empty"]
    elif case == "false_empty":
        evidence["display_empty"] = False
    seq = await boundary(admin, status="failed", extra=evidence)
    with pytest.raises(ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval("SELECT status FROM tasks") == "running"
    assert (
        await admin.fetchval("SELECT last_runtime_seq FROM runtime_projection_cursors")
        == seq - 1
    )
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM product_events WHERE source_runtime_event_id=$1",
            str(seq),
        )
        == 0
    )
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM task_events WHERE event_type='run_finished'"
        )
        == 0
    )


@pytest.mark.parametrize(
    "case", ["final_cut", "complete_cut", "wrong_boundary_status", "internal_extra"]
)
async def test_fault_cannot_claim_complete_display_or_change_its_boundary(
    projection_database, case
):
    sessions, admin = projection_database
    evidence = await prepare(
        sessions,
        admin,
        reason="error",
        body=True,
        phase="final" if case == "final_cut" else "interrupted",
        incomplete=case not in {"complete_cut", "final_cut"},
    )
    if case == "internal_extra":
        evidence["execution_failure"]["exception"] = "PRIVATE_EXCEPTION_WITH_CREDENTIAL"
    seq = await boundary(admin, status="failed", extra=evidence)
    if case == "wrong_boundary_status":
        await admin.execute(
            "UPDATE runtime_events SET payload=jsonb_set(payload,'{status}','\"completed\"') WHERE id=$1",
            str(seq),
        )
    from app.adapter.durable_projection import ProjectionDeferred
    expected=ProjectionDeferred if case=='final_cut' else ProjectionConflict
    with pytest.raises(expected):
        await consume(sessions)
    assert await admin.fetchval("SELECT status FROM tasks") == "running"
    assert (
        await admin.fetchval("SELECT last_runtime_seq FROM runtime_projection_cursors")
        == seq - 1
    )
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM product_events WHERE source_runtime_event_id=$1",
            str(seq),
        )
        == 0
    )


async def test_user_stop_wins_over_verified_realtime_failure_without_losing_partial_body(
    projection_database,
):
    sessions, admin = projection_database
    evidence = await prepare(sessions, admin, reason="interrupted", body=True)
    await admin.execute("UPDATE tasks SET status='cancelling' WHERE id='t'")
    seq = await boundary(admin, status="failed", extra=evidence)
    await consume(sessions)
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'") == "cancelled"
    public = [
        json.loads(row["payload"])
        for row in await admin.fetch(
            "SELECT payload FROM product_events WHERE source_runtime_event_id=$1 ORDER BY product_seq",
            str(seq),
        )
    ]
    assert sum(item["type"] == "RUN_CANCELLED" for item in public) == 1
    assert not any(item["type"] in {"RUN_ERROR", "RUN_FINISHED"} for item in public)
    completed = next(item for item in public if item.get("name") == "TASK_COMPLETED")
    assert completed["value"]["taskStatus"] == "cancelled"
    assert completed["value"]["reason"] == "interrupted"
    assert completed["value"]["completionGate"] == "realtime_unavailable"
    assert all(
        item.get("display_snapshot") == evidence["display_snapshot"] for item in public
    )
    row = await admin.fetchrow(
        "SELECT content,snapshot_phase,snapshot_incomplete FROM task_messages WHERE role='assistant'"
    )
    assert json.loads(row["content"])["text"] == "已保存的正文🙂"
    assert row["snapshot_phase"] == "interrupted" and row["snapshot_incomplete"] is True
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM task_events WHERE event_type='task_cancelled'"
        )
        == 1
    )


async def test_explicit_null_execution_failure_is_rejected_instead_of_treated_as_absent(
    projection_database,
):
    sessions, admin = projection_database
    evidence = await prepare(sessions, admin, reason="error", body=False)
    evidence["execution_failure"] = None
    seq = await boundary(admin, status="failed", extra=evidence)
    with pytest.raises(ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval("SELECT status FROM tasks") == "running"
    assert (
        await admin.fetchval("SELECT last_runtime_seq FROM runtime_projection_cursors")
        == seq - 1
    )
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM product_events WHERE source_runtime_event_id=$1",
            str(seq),
        )
        == 0
    )
