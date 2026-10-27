"""Read the exact, durable HITL grant for a resource submission."""

import json

from fastapi import HTTPException
from sqlalchemy import select

from workmate_product.approval import approval_request_version
from workmate_product.persistence.models import TaskEvent


async def approved_submission(db, task_id, call_id, tool_name, user_id) -> dict:
    events = (await db.scalars(select(TaskEvent).where(
        TaskEvent.task_id == task_id,
        TaskEvent.event_type.in_(["hitl_request", "hitl_decision"]),
    ).order_by(TaskEvent.seq.desc()))).all()
    for request in events:
        if request.event_type != "hitl_request":
            continue
        calls = request.payload.get("toolCalls", [])
        call = next((item for item in calls if item.get("id") == call_id), None)
        if call is None:
            continue
        version = approval_request_version(request.payload.get("replyId", ""), calls)
        for decision in events:
            if decision.seq <= request.seq or decision.event_type != "hitl_decision":
                continue
            payload = decision.payload
            if payload.get("requestVersion") != version or payload.get("actorUserId") != user_id:
                continue
            grant = next((item for item in payload.get("decisions", [])
                          if item.get("toolCallId") == call_id), None)
            if grant is None:
                continue
            if grant.get("approved") is not True or call.get("name") != tool_name:
                break
            raw = call.get("input", {})
            try:
                value = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                break
            if isinstance(value, dict):
                return value
            break
        break
    raise HTTPException(status_code=403, detail="Explicit submission confirmation required")
