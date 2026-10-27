"""Explicit fresh executions; original ownership and memory namespaces survive."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, _RUNTIME_JSON


class RecoveryFork(Base):
    __tablename__ = 'recovery_forks'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey('conversations.id'))
    source_conversation_id: Mapped[str] = mapped_column(ForeignKey('conversations.id'))
    source_session_id: Mapped[str] = mapped_column(ForeignKey('runtime_sessions.id'), unique=True)
    target_session_id: Mapped[str] = mapped_column(ForeignKey('runtime_sessions.id'), unique=True)
    context_text: Mapped[str] = mapped_column(Text)
    source_task_id: Mapped[str] = mapped_column(ForeignKey('tasks.id'))
    target_task_id: Mapped[str] = mapped_column(String(36))
    target_node_id: Mapped[str] = mapped_column(String(128))
    actor_user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    request_key: Mapped[str] = mapped_column(String(36))
    checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey('workspace_checkpoints.id'))
    manifest: Mapped[dict] = mapped_column(_RUNTIME_JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint('actor_user_id','request_key', name='uq_recovery_fork_request'),)
