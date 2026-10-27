"""HTTP-facing task controls; shared Product owns the business use cases."""
from workmate_product.application.task_controls import TaskControls, TaskControlConflict
from .runtime_errors import ApprovalConflict
from fastapi import HTTPException
from workmate_product.application.task_capacity import TaskCapacityPolicy, TaskCapacityExceeded


class ProductRuntime(TaskControls):
    def __init__(self, sessions, admission, *, knowledge_enabled=False, capacity_policy=TaskCapacityPolicy()):
        super().__init__(knowledge_enabled=knowledge_enabled, capacity_policy=capacity_policy)
        self.sessions, self.admission = sessions, admission

    async def create_task(self, *args, **kwargs):
        return await self.admission.create_task(*args, **kwargs)

    async def confirm(self, *args, **kwargs):
        try:
            return await super().confirm(*args, **kwargs)
        except TaskCapacityExceeded as error:
            raise HTTPException(429, detail=str(error), headers={'Retry-After': '5'}) from error
        except TaskControlConflict as error:
            raise ApprovalConflict(str(error)) from error

    async def stop(self, *args, **kwargs):
        try:
            return await super().stop(*args, **kwargs)
        except TaskControlConflict as error:
            raise ApprovalConflict(str(error)) from error
