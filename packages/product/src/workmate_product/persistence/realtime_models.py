"""Durable log identity and reliable business notifications, never token history."""
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base


class ConversationEventLog(Base):
    __tablename__='conversation_event_logs'
    conversation_id: Mapped[str]=mapped_column(ForeignKey('conversations.id'),primary_key=True)
    generation: Mapped[str]=mapped_column(String(36))
    version: Mapped[int]=mapped_column(BigInteger)
    status: Mapped[str]=mapped_column(String(24))
    initialization_token: Mapped[str]=mapped_column(String(36))
    lease_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    incarnation: Mapped[str|None]=mapped_column(String(36))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
    __table_args__=(
        CheckConstraint('version>0',name='ck_event_log_version'),
        CheckConstraint("status IN ('initializing','active')",name='ck_event_log_status'),
        CheckConstraint("(status='active' AND incarnation IS NOT NULL AND lease_until IS NULL) OR "
            "(status='initializing' AND incarnation IS NULL AND lease_until IS NOT NULL)",name='ck_event_log_activation'),
    )


class RealtimeBusinessOutbox(Base):
    __tablename__='realtime_business_outbox'
    event_id: Mapped[str]=mapped_column(ForeignKey('product_events.id'),primary_key=True)
    conversation_id: Mapped[str]=mapped_column(ForeignKey('conversations.id'))
    business_seq: Mapped[int]=mapped_column(BigInteger)
    status: Mapped[str]=mapped_column(String(24),server_default='pending')
    claim_token: Mapped[str|None]=mapped_column(String(36))
    attempt_count: Mapped[int]=mapped_column(Integer,server_default='0')
    lease_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
    published_generation: Mapped[str|None]=mapped_column(String(36))
    published_incarnation: Mapped[str|None]=mapped_column(String(36))
    published_stream_id: Mapped[str|None]=mapped_column(String(41))
    error_code: Mapped[str|None]=mapped_column(String(64))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
    published_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    __table_args__=(
        CheckConstraint('business_seq>0 AND business_seq<=9007199254740991 AND attempt_count>=0',name='ck_realtime_outbox_versions'),
        CheckConstraint("status IN ('pending','publishing','published','blocked')",name='ck_realtime_outbox_status'),
        CheckConstraint("(status='publishing' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status<>'publishing' AND claim_token IS NULL AND lease_until IS NULL)",name='ck_realtime_outbox_claim'),
        CheckConstraint("status<>'published' OR (published_generation IS NOT NULL AND published_incarnation IS NOT NULL "
            "AND published_stream_id IS NOT NULL AND published_at IS NOT NULL)",name='ck_realtime_outbox_receipt'),
        UniqueConstraint('conversation_id','business_seq',name='uq_realtime_outbox_business_seq'),
        Index('ix_realtime_outbox_due','status','next_attempt_at'),
    )
