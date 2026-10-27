"""Verified, immutable file checkpoints; not execution or Core snapshots."""
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, _RUNTIME_JSON


class WorkspaceCheckpoint(Base):
    __tablename__ = 'workspace_checkpoints'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey('conversations.id'))
    task_id: Mapped[str] = mapped_column(ForeignKey('tasks.id'))
    session_id: Mapped[str] = mapped_column(String(128))
    execution_epoch: Mapped[int] = mapped_column(BigInteger)
    storage_node_id: Mapped[str] = mapped_column(String(128))
    boundary: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    manifest: Mapped[dict | None] = mapped_column(_RUNTIME_JSON)
    digest: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("boundary IN ('terminal','approval')", name='ck_workspace_checkpoint_boundary'),
        CheckConstraint("(status='available' AND manifest IS NOT NULL AND digest IS NOT NULL AND error_code IS NULL) OR "
                        "(status='unavailable' AND manifest IS NULL AND digest IS NULL AND error_code IS NOT NULL)",
                        name='ck_workspace_checkpoint_result'),
        Index('ix_workspace_checkpoint_conversation', 'conversation_id', 'created_at'),
    )
