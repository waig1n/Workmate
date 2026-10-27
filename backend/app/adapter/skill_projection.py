"""Skill source is Runtime input; browsers receive only execution facts."""
from sqlalchemy import func, or_, select

from workmate_product.persistence.models import TaskEvent

SKILL_TOOL_NAME = 'Skill'


def is_skill_payload(payload):
    return any(payload.get(key) == SKILL_TOOL_NAME for key in ('tool', 'tool_call_name', 'toolCallName'))


def compact_skill_payload(payload):
    return {**{key: payload[key] for key in (
        'toolCallId', 'tool_call_id', 'status', 'state', 'phase', 'reply_id',
    ) if key in payload}, 'tool': SKILL_TOOL_NAME, 'content_hidden': True}


async def historical_skill_event_ids(db, task_id):
    """Classify legacy results even when filtered paging omits their named start.

    Read only event IDs; raw historical content remains untouched in storage.
    Classify both raw facts and old browser projections: either can carry the
    only surviving tool identity in a historical row.
    """
    payloads = (TaskEvent.payload, TaskEvent.payload['browser_projection'])
    named_skill = or_(*(payload[key].as_string() == SKILL_TOOL_NAME
                        for payload in payloads
                        for key in ('tool', 'tool_call_name', 'toolCallName')))
    tool_id = func.coalesce(*(payload[key].as_string()
                              for payload in payloads
                              for key in ('toolCallId', 'tool_call_id')))
    named_ids = select(tool_id).where(
        TaskEvent.task_id == task_id, named_skill, tool_id.is_not(None),
    )
    return set((await db.scalars(select(TaskEvent.id).where(
        TaskEvent.task_id == task_id,
        or_(named_skill, tool_id.in_(named_ids)),
    ))).all())
