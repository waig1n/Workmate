"""Independent active-work and pending-approval admission budgets."""
from dataclasses import dataclass
from datetime import timezone

from .commands import CommandRejected
from ..persistence.repositories import task_capacity as repository


class TaskCapacityExceeded(CommandRejected):
    pass


@dataclass(frozen=True)
class TaskCapacityPolicy:
    task_max_outstanding: int = 64
    task_max_outstanding_per_user: int = 8
    task_max_pending_approval: int = 256
    task_max_pending_approval_per_user: int = 32
    approval_timeout_secs: int = 86400

    @classmethod
    def from_settings(cls, settings):
        return cls(**{name: getattr(settings, name) for name in cls.__dataclass_fields__})

    async def check(self, db, user_id, *, resuming_task_id=None):
        active, own, pending, own_pending = await repository.counts(
            db, user_id, exclude_task_id=resuming_task_id)
        if active >= self.task_max_outstanding or own >= self.task_max_outstanding_per_user:
            raise TaskCapacityExceeded('活跃任务已达容量上限，请稍后重试；可停止不再需要的任务')
        if resuming_task_id is None and (pending >= self.task_max_pending_approval
                or own_pending >= self.task_max_pending_approval_per_user):
            raise TaskCapacityExceeded('待审批任务已达容量上限，请处理或停止待审批任务后重试')

    async def expired(self, db, task_id, version):
        requested = await repository.request_time(db, task_id, version)
        if requested is None:
            raise CommandRejected('Approval request has no durable timestamp')
        requested = requested.replace(tzinfo=requested.tzinfo or timezone.utc)
        # Database time avoids clock skew across Gateway replicas.
        now = await repository.database_time(db)
        return (now - requested).total_seconds() >= self.approval_timeout_secs
