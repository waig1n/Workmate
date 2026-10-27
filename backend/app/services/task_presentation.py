"""Task API presentation; transient execution detail never changes TaskStatus."""
from sqlalchemy.ext.asyncio import AsyncSession
from workmate_product.persistence.models import Task
from workmate_product.persistence.task_execution_phase import read_execution_phases
from ..api.schemas import TaskOut


async def present_tasks(db: AsyncSession, tasks: list[Task]) -> list[TaskOut]:
    phases = await read_execution_phases(db, [task.id for task in tasks if task.status == 'created'])
    return [TaskOut.model_validate(task).model_copy(update={
        'execution_phase': phases.get(task.id, 'queued') if task.status == 'created' else None,
    }) for task in tasks]


async def present_task(db: AsyncSession, task: Task) -> TaskOut:
    return (await present_tasks(db, [task]))[0]
