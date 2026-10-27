"""Workspace compute holdings and exact release receipts, independent of Task state."""
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, _RUNTIME_JSON


class WorkspaceResidency(Base):
    __tablename__='workspace_residencies'
    session_id: Mapped[str]=mapped_column(ForeignKey('runtime_sessions.id'),primary_key=True)
    storage_node_id: Mapped[str]=mapped_column(String(128))
    workspace_generation: Mapped[str]=mapped_column(String(128))
    holder_worker_id: Mapped[str]=mapped_column(String(128))
    hold_version: Mapped[int]=mapped_column(BigInteger)
    status: Mapped[str]=mapped_column(String(24))
    release_request_id: Mapped[str|None]=mapped_column(String(128))
    last_task_id: Mapped[str]=mapped_column(ForeignKey('tasks.id'))
    last_execution_epoch: Mapped[int]=mapped_column(BigInteger)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
    __table_args__=(
        CheckConstraint('hold_version>0 AND last_execution_epoch>0',name='ck_workspace_residency_versions'),
        CheckConstraint("status IN ('held','releasing','released','blocked')",name='ck_workspace_residency_status'),
        CheckConstraint("status='held' OR release_request_id IS NOT NULL",name='ck_workspace_residency_release'),
        Index('ix_workspace_residency_holder','storage_node_id','holder_worker_id','status'),
    )


class WorkspaceReleaseRequest(Base):
    __tablename__='workspace_release_requests'
    id: Mapped[str]=mapped_column(String(128),primary_key=True)
    session_id: Mapped[str]=mapped_column(ForeignKey('runtime_sessions.id'))
    storage_node_id: Mapped[str]=mapped_column(String(128))
    expected_worker_id: Mapped[str]=mapped_column(String(128))
    expected_hold_version: Mapped[int]=mapped_column(BigInteger)
    workspace_generation: Mapped[str]=mapped_column(String(128))
    reason: Mapped[str]=mapped_column(String(32),server_default='idle')
    status: Mapped[str]=mapped_column(String(24),server_default='pending')
    cleanup_receipt: Mapped[dict|None]=mapped_column(_RUNTIME_JSON)
    error_code: Mapped[str|None]=mapped_column(String(128))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
    completed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    __table_args__=(
        UniqueConstraint('session_id','expected_hold_version',name='uq_workspace_release_hold'),
        CheckConstraint('expected_hold_version>0',name='ck_workspace_release_version'),
        CheckConstraint("status IN ('pending','releasing','released','stale','blocked')",name='ck_workspace_release_status'),
        CheckConstraint("reason IN ('idle','execution_cleanup')",name='ck_workspace_release_reason'),
        CheckConstraint("(status='released' AND cleanup_receipt IS NOT NULL AND completed_at IS NOT NULL) OR "
            "(status<>'released' AND cleanup_receipt IS NULL)",name='ck_workspace_release_receipt'),
        Index('ix_workspace_release_pending','storage_node_id','expected_worker_id','status'),
    )
