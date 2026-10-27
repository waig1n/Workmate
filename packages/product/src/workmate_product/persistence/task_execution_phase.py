"""Batched read model for execution progress before ReplyStart projection."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .models import RuntimeSession, RuntimeTaskExecution


async def read_execution_phases(db: AsyncSession, task_ids: list[str]) -> dict[str, str]:
    if not task_ids:
        return {}
    rows = await db.execute(
        select(RuntimeTaskExecution.task_id, RuntimeTaskExecution.status)
        .join(RuntimeSession, RuntimeSession.id == RuntimeTaskExecution.session_id)
        .where(
            RuntimeTaskExecution.task_id.in_(task_ids),
            RuntimeSession.active_task_id == RuntimeTaskExecution.task_id,
            RuntimeSession.execution_epoch == RuntimeTaskExecution.execution_epoch,
            RuntimeSession.status == RuntimeTaskExecution.status,
            RuntimeSession.owner_id.is_not(None),
            RuntimeTaskExecution.status.in_(['preparing', 'running']),
        )
    )
    return dict(rows.all())
