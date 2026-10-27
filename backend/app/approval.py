# -*- coding: utf-8 -*-
"""Product-owned approval profiles and HITL idempotency helpers.

The browser only uses stable product vocabulary.  AgentScope permission-mode
names remain an adapter detail so a runtime upgrade cannot silently redefine
the public API.
"""

from __future__ import annotations

from workmate_product.approval import approval_request_version

from sqlalchemy import select

from workmate_product.persistence.models import ApprovalProfile, TaskEvent


APPROVAL_PROFILE_PERMISSION_MODES: dict[ApprovalProfile, str] = {
    ApprovalProfile.STANDARD: "accept_edits",
    ApprovalProfile.STRICT: "default",
    ApprovalProfile.READ_ONLY: "explore",
    ApprovalProfile.FULL_ACCESS: "bypass",
    ApprovalProfile.UNATTENDED: "dont_ask",
}


async def approval_was_decided(
    db,
    decision_version: str,
    *,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> bool:
    """Check the append-only audit log for an answered HITL request.

    ``Task.last_decision_version`` is only a fast pointer to the most recent
    answer. A reply may park more than once, so restart/replay de-duplication
    must use the complete durable decision history.
    """
    return await _version_in_audit_log(
        db,
        decision_version,
        event_types=("hitl_decision",),
        conversation_id=conversation_id,
        task_id=task_id,
    )


async def approval_resolved(
    db,
    decision_version: str,
    *,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> bool:
    """Check whether a HITL request reached any terminal outcome.

    A request is resolved when it was decided (``hitl_decision``),
    abandoned (``hitl_abandoned`` tombstone written in the same
    transaction as the Task's terminal status), or closed as
    undeliverable (``hitl_resume_exhausted``: decided but the resume
    can never complete - reconciliation seals it once the runtime
    confirms idle). A replayed ``REQUIRE_USER_CONFIRM`` matching a
    resolved version must not re-park the Task or re-broadcast the
    approval card.
    """
    return await _version_in_audit_log(
        db,
        decision_version,
        event_types=(
            "hitl_decision",
            "hitl_abandoned",
            "hitl_resume_exhausted",
        ),
        conversation_id=conversation_id,
        task_id=task_id,
    )


async def approval_was_abandoned(
    db,
    decision_version: str,
    *,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> bool:
    """Check whether a HITL request was abandoned (tombstone only).

    Distinct from :func:`approval_resolved`: a decided request on a Task
    that is still ``awaiting_permission`` is the retryable-resume path -
    its card must stay rebuildable via ``pending-confirm`` and the
    reconcile repair. Only the ``hitl_abandoned`` tombstone means the
    request died with its run and must not come back.
    """
    return await _version_in_audit_log(
        db,
        decision_version,
        event_types=("hitl_abandoned",),
        conversation_id=conversation_id,
        task_id=task_id,
    )


async def approval_resume_exhausted(
    db,
    decision_version: str,
    *,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> bool:
    """Check whether a DECIDED request was closed as undeliverable.

    Written by reconciliation when the runtime authoritatively reports
    idle for a request whose decision is durable but whose resume can
    never complete. Mutually exclusive with ``hitl_abandoned``: an
    undecided request is abandoned, a decided one is exhausted. Both
    close the request and must block probe-flap restoration.
    """
    return await _version_in_audit_log(
        db,
        decision_version,
        event_types=("hitl_resume_exhausted",),
        conversation_id=conversation_id,
        task_id=task_id,
    )


async def approval_request_was_decided(
    db,
    *,
    task_id: str,
    tool_call_ids,
) -> bool:
    """Whether a durable decision answered THIS request.

    Matches by tool call ids, not by version: a client may submit its
    own ``decision_version`` (idempotency key), so a decision's version
    string is not guaranteed to equal the request's canonical version.
    A decision answers the request when it covers every tool call the
    request asked about.
    """
    wanted = {str(value) for value in tool_call_ids}
    if not wanted:
        return False
    payloads = await db.scalars(
        select(TaskEvent.payload).where(
            TaskEvent.event_type == "hitl_decision",
            TaskEvent.task_id == task_id,
        ),
    )
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        decided_ids = {
            str(item.get("toolCallId"))
            for item in payload.get("decisions", [])
            if isinstance(item, dict)
        }
        if wanted <= decided_ids:
            return True
    return False


async def _version_in_audit_log(
    db,
    decision_version: str,
    *,
    event_types: tuple[str, ...],
    conversation_id: str | None,
    task_id: str | None,
) -> bool:
    if not decision_version or (conversation_id is None and task_id is None):
        return False
    query = select(TaskEvent.payload).where(
        TaskEvent.event_type.in_(event_types),
    )
    if conversation_id is not None:
        query = query.where(TaskEvent.conversation_id == conversation_id)
    if task_id is not None:
        query = query.where(TaskEvent.task_id == task_id)
    payloads = await db.scalars(query)
    return any(
        isinstance(payload, dict)
        and payload.get("decisionVersion") == decision_version
        for payload in payloads
    )


async def approval_was_requested(
    db,
    decision_version: str,
    *,
    task_id: str,
) -> bool:
    """Check the audit log for an already-recorded request version.

    Used to de-duplicate ``hitl_request`` events: the upstream replay
    log can re-deliver an undecided ``REQUIRE_USER_CONFIRM`` while the
    Task is still parked, which must refresh the in-memory pending
    state without appending a second audit event. Legacy payloads
    without an explicit version are recomputed from their reply and
    tool calls.
    """
    if not decision_version:
        return False
    payloads = await db.scalars(
        select(TaskEvent.payload).where(
            TaskEvent.event_type == "hitl_request",
            TaskEvent.task_id == task_id,
        ),
    )
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        version = payload.get("decisionVersion")
        if version is None:
            tool_calls = payload.get("toolCalls")
            version = approval_request_version(
                payload.get("replyId") or "",
                tool_calls if isinstance(tool_calls, list) else [],
            )
        if version == decision_version:
            return True
    return False


def permission_mode_for(profile: ApprovalProfile | str) -> str:
    return APPROVAL_PROFILE_PERMISSION_MODES[ApprovalProfile(profile)]
