"""Dispatch intent and transport quarantine, independent of execution authority."""
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base


class CommandDelivery(Base):
    __tablename__ = 'command_deliveries'
    command_id: Mapped[str] = mapped_column(ForeignKey('runtime_commands.id'), primary_key=True)
    delivery_generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    retry_origin_generation: Mapped[int] = mapped_column(BigInteger, server_default='1')
    storage_node_id: Mapped[str] = mapped_column(String(128))
    target_incarnation: Mapped[str | None] = mapped_column(String(128))
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), server_default='pending')
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempt_count: Mapped[int] = mapped_column(Integer, server_default='0')
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stream_message_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint('delivery_generation>0 AND attempt_count>=0 AND retry_origin_generation>0 AND retry_origin_generation<=delivery_generation', name='ck_command_delivery_versions'),
        CheckConstraint("channel IN ('execute','control')", name='ck_command_delivery_channel'),
        CheckConstraint("status IN ('pending','publishing','published','superseded','settled','quarantined')", name='ck_command_delivery_status'),
        CheckConstraint("(status='publishing' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status<>'publishing' AND lease_token IS NULL AND lease_until IS NULL)", name='ck_command_delivery_lease'),
        Index('ix_command_deliveries_due','status','next_attempt_at'),
    )


class CommandDeliveryQuarantine(Base):
    __tablename__ = 'command_delivery_quarantines'
    stream_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    command_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
