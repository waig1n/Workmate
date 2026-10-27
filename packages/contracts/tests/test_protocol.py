import pytest
from pydantic import ValidationError

from workmate_contracts import (
    Command, PendingInteraction, ProductCursor, ProductEvent, RuntimeEvent,
    TaskExecution,
)


def command(**changes):
    return Command.model_validate({**dict(
        command_id="command-1", kind="StartTask", conversation_id="conversation-1",
        product_task_id="task-1", runtime_session_id="session-1",
        payload={"input": "hello"},
    ), **changes})


def test_content_digest_is_canonical_and_includes_identity():
    first = command()
    assert first.content_digest() == command().content_digest()
    assert first.content_digest() != command(expected_epoch=2).content_digest()
    assert Command.model_validate(first.model_dump()).content_digest() == first.content_digest()


@pytest.mark.parametrize("changes", [
    {"protocol_version": 2}, {"unexpected": True}, {"expected_epoch": -1},
    {"kind": "Unknown"}, {"reply_id": ""},
])
def test_invalid_protocol_fails_closed(changes):
    with pytest.raises(ValidationError):
        command(**changes)


def test_payload_limit_is_measured_in_utf8_bytes():
    with pytest.raises(ValidationError):
        Command.model_validate({**command().model_dump(), "payload": {"input": "中" * 100_000}})


@pytest.mark.parametrize("value", [object(), (1, 2), float("nan"), float("inf")])
def test_payload_rejects_non_json_values(value):
    with pytest.raises(ValidationError):
        command(payload={"invalid": value})


def test_browser_cursor_cannot_accept_runtime_sequence():
    cursor = ProductCursor(conversation_id="c", product_seq=0)
    assert cursor.product_seq == 0
    with pytest.raises(ValidationError):
        ProductCursor(conversation_id="c", runtime_seq=1)


@pytest.mark.parametrize("kind,values", [
    ("StartTask", {"product_task_id": None}),
    ("ResumeInteraction", {}),
    ("ResumeInteraction", {"reply_id": "r", "interaction_id": "i"}),
])
def test_task_and_resume_binding_are_required(kind, values):
    with pytest.raises(ValidationError):
        command(kind=kind, **values)


def test_prepare_and_resume_envelopes():
    assert command(kind="PrepareSession", product_task_id=None).product_task_id is None
    assert command(kind="ResumeInteraction", reply_id="r", interaction_id="i",
                   request_version="approval-digest").request_version == "approval-digest"


def test_request_digest_is_not_a_database_revision():
    with pytest.raises(ValidationError):
        command(kind="ResumeInteraction", reply_id="r", interaction_id="i", request_version=1)


def execution(**changes):
    return TaskExecution.model_validate({
        "conversation_id": "c", "runtime_session_id": "s", "product_task_id": "t",
        "status": "accepted", "state_version": 0, "execution_epoch": 0,
        **changes,
    })


def test_execution_query_distinguishes_waiting_from_preparing_and_running():
    assert execution().reply_id is None
    assert execution(status="preparing", execution_epoch=1).status == "preparing"
    assert execution(status="running", execution_epoch=1, reply_id="r").status == "running"
    with pytest.raises(ValidationError):
        execution(status="running", reply_id="r")


def test_parked_query_keeps_pending_tool_identity_after_partial_confirmation():
    pending = PendingInteraction(
        interaction_id="i", request_version="approval-digest", kind="permission",
        pending_tool_call_ids=["second-tool"],
    )
    value = execution(status="awaiting_interaction", execution_epoch=1,
                      reply_id="original-reply", pending_interaction=pending)
    assert TaskExecution.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError):
        execution(status="awaiting_interaction", execution_epoch=1, reply_id="r")
    with pytest.raises(ValidationError):
        execution(status="completed", execution_epoch=1, pending_interaction=pending)


def test_assembly_failure_can_finish_without_reply():
    assert execution(status="failed", execution_epoch=1).reply_id is None
    with pytest.raises(ValidationError):
        execution(status="completed", execution_epoch=1)


def test_stop_before_claim_is_valid_but_parked_stop_requires_execution_identity():
    assert execution(status="stopping").reply_id is None
    pending = PendingInteraction(interaction_id="i", request_version="digest", kind="permission",
                                 pending_tool_call_ids=["call"])
    assert execution(status="stopping", reply_id="r", execution_epoch=1,
                     pending_interaction=pending).pending_interaction == pending
    for fields in ({}, {"reply_id": "r"}, {"execution_epoch": 1}):
        with pytest.raises(ValidationError):
            execution(status="stopping", pending_interaction=pending, **fields)


@pytest.mark.parametrize("ids", [[], ["duplicate", "duplicate"]])
def test_pending_tool_ids_cannot_be_empty_or_duplicate(ids):
    with pytest.raises(ValidationError):
        PendingInteraction(interaction_id="i", request_version="digest", kind="permission",
                           pending_tool_call_ids=ids)


def test_two_sequence_domains_survive_wire_round_trip():
    runtime = RuntimeEvent(
        runtime_event_id="re", runtime_session_id="s", runtime_seq=17,
        execution_epoch=2, conversation_id="c", product_task_id="t",
        event_type="execution.started", occurred_at="2026-09-15T00:00:00Z",
    )
    product = ProductEvent(
        product_event_id="pe", conversation_id="c", product_seq=23,
        source_runtime_event_id=runtime.runtime_event_id, product_task_id="t",
        event_type="task.started", occurred_at=runtime.occurred_at,
    )
    assert RuntimeEvent.model_validate_json(runtime.model_dump_json()) == runtime
    assert ProductEvent.model_validate_json(product.model_dump_json()) == product
    with pytest.raises(ValidationError):
        ProductEvent.model_validate(runtime.model_dump())


def test_event_time_requires_timezone():
    with pytest.raises(ValidationError):
        ProductEvent(product_event_id="pe", conversation_id="c", product_seq=1,
                     event_type="task.stopped", occurred_at="2026-09-15T00:00:00")


def test_boundary_evidence_is_strict_unique_and_bound_to_snapshot():
    from workmate_contracts import BoundaryEvidence, PendingToolCall, snapshot_digest
    snapshot={'state':{'session_id':'s'},'format_version':1}
    pending=PendingToolCall(id='call',name='write',input='{"x":1}',kind='permission')
    value=BoundaryEvidence(snapshot_sha256=snapshot_digest(snapshot),pending_tools=[pending])
    assert BoundaryEvidence.model_validate_json(value.model_dump_json())==value
    assert snapshot_digest(snapshot)!=snapshot_digest({'state':{'session_id':'other'},'format_version':1})
    for overrides in ({'pending_tools':[pending,pending]}, {'snapshot_sha256':'bad'}):
        with pytest.raises(ValidationError):
            BoundaryEvidence.model_validate({**value.model_dump(),**overrides})
    with pytest.raises(ValidationError):
        PendingToolCall(id='call',name='write',input={},kind='permission')


def test_snapshot_digest_survives_jsonb_numeric_normalization():
    from workmate_contracts import snapshot_digest
    assert snapshot_digest({'x':1e20,'y':-0.0})==snapshot_digest({'y':0,'x':100000000000000000000})
    assert snapshot_digest({'x':True})!=snapshot_digest({'x':1})
    with pytest.raises(ValueError):
        snapshot_digest({'x':float('nan')})
