"""Durable permission-pause facts; no automatic execution takeover."""
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, _RUNTIME_JSON


class RuntimeSuspension(Base):
    __tablename__ = 'runtime_suspensions'
    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128))
    task_id: Mapped[str] = mapped_column(String(36))
    reply_id: Mapped[str] = mapped_column(String(128))
    storage_node_id: Mapped[str] = mapped_column(String(128))
    source_worker_id: Mapped[str] = mapped_column(String(128))
    source_epoch: Mapped[int] = mapped_column(BigInteger)
    state_version: Mapped[int] = mapped_column(BigInteger)
    runtime_seq: Mapped[int] = mapped_column(BigInteger)
    resource_generation: Mapped[str] = mapped_column(String(128))
    snapshot_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    cleanup_receipt: Mapped[dict | None] = mapped_column(_RUNTIME_JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_worker_id: Mapped[str | None] = mapped_column(String(128))
    resume_epoch: Mapped[int | None] = mapped_column(BigInteger)
    resume_command_id: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        CheckConstraint('(resume_worker_id IS NULL AND resume_epoch IS NULL AND resume_command_id IS NULL) OR '
            "(status='released' AND resume_worker_id IS NOT NULL AND resume_command_id IS NOT NULL AND resume_epoch=source_epoch+1)",
            name='ck_runtime_suspension_claim'),
        ForeignKeyConstraint(['snapshot_id','task_id','session_id','state_version'],
            ['runtime_snapshots.id','runtime_snapshots.task_id','runtime_snapshots.session_id','runtime_snapshots.state_version'],
            name='fk_runtime_suspension_boundary'),
        CheckConstraint('source_epoch>0 AND state_version>0 AND runtime_seq>0', name='ck_runtime_suspension_versions'),
        CheckConstraint("status IN ('releasing','released','blocked')", name='ck_runtime_suspension_status'),
        CheckConstraint('length(snapshot_digest)=64 AND length(resource_generation)>0', name='ck_runtime_suspension_proof'),
        CheckConstraint("(status='released' AND cleanup_receipt IS NOT NULL AND released_at IS NOT NULL) OR "
            "(status IN ('releasing','blocked') AND cleanup_receipt IS NULL AND released_at IS NULL)",
            name='ck_runtime_suspension_release'),
        Index('ix_runtime_suspension_node_status','storage_node_id','status'),
    )
