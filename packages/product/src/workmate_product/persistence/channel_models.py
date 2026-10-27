"""Product IM connection, identity and durable delivery facts."""
from sqlalchemy import Column, String, Text, Integer, BigInteger, Boolean, JSON, DateTime, ForeignKey, UniqueConstraint, CheckConstraint, Index, func
from .models import Base

class ChannelConnection(Base):
    __tablename__ = 'channel_connections'
    id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False)
    provider = Column(String(16), nullable=False)
    agent_id = Column(String(36), ForeignKey('agents.id'), nullable=False)
    scope_id = Column(String(36), ForeignKey('scopes.id'), nullable=False)
    owner_user_id = Column(String(36), ForeignKey('users.id'))
    credentials = Column(JSON, nullable=False, default=dict, server_default='{}')
    bot_key = Column(String(128), unique=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default='false')
    group_enabled = Column(Boolean, nullable=False, default=False, server_default='false')
    group_allowlist = Column(JSON, nullable=False, default=list, server_default='[]')
    version = Column(Integer, nullable=False, default=1, server_default='1')
    status = Column(String(24), nullable=False, default='stopped', server_default='stopped')
    last_error = Column(Text)
    deleted_at = Column(DateTime(timezone=True))
    lease_owner = Column(String(128))
    lease_until = Column(DateTime(timezone=True))
    lease_epoch = Column(BigInteger, nullable=False, default=0, server_default='0')
    cursor = Column(Text, nullable=False, default='', server_default='')
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        CheckConstraint("provider IN ('wecom','feishu','dingtalk','wechat') AND version>0 AND lease_epoch>=0", name='ck_channel_connection'),
    )


class ChannelIdentity(Base):
    __tablename__ = 'channel_identities'
    id = Column(String(36), primary_key=True)
    channel_id = Column(String(36), ForeignKey('channel_connections.id'), nullable=False)
    external_user_id = Column(String(256), nullable=False)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        UniqueConstraint('channel_id','external_user_id', name='uq_channel_identity'),
    )


class ChannelBindingCode(Base):
    __tablename__ = 'channel_binding_codes'
    code_hash = Column(String(64), primary_key=True)
    channel_id = Column(String(36), ForeignKey('channel_connections.id'), nullable=False)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed_at = Column(DateTime(timezone=True))


class ChannelConversation(Base):
    __tablename__ = 'channel_conversations'
    id = Column(String(36), primary_key=True)
    channel_id = Column(String(36), ForeignKey('channel_connections.id'), nullable=False)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)
    agent_id = Column(String(36), ForeignKey('agents.id'), nullable=False)
    chat_key = Column(String(512), nullable=False)
    conversation_id = Column(String(36), ForeignKey('conversations.id'), nullable=False)
    __table_args__ = (
        UniqueConstraint('channel_id','user_id','agent_id','chat_key', name='uq_channel_conversation'),
    )


class ChannelInbox(Base):
    __tablename__ = 'channel_inbox'
    id = Column(String(36), primary_key=True)
    channel_id = Column(String(36), ForeignKey('channel_connections.id'), nullable=False)
    channel_version = Column(Integer, nullable=False)
    message_id = Column(String(256), nullable=False)
    sender_id = Column(String(256), nullable=False)
    chat_id = Column(String(256), nullable=False)
    chat_type = Column(String(16), nullable=False)
    text = Column(Text, nullable=False)
    target = Column(JSON, nullable=False)
    mentioned = Column(Boolean, nullable=False, default=False, server_default='false')
    unsupported = Column(Boolean, nullable=False, default=False, server_default='false')
    status = Column(String(16), nullable=False, default='pending', server_default='pending')
    user_id = Column(String(36), ForeignKey('users.id'))
    conversation_id = Column(String(36), ForeignKey('conversations.id'))
    task_id = Column(String(36), ForeignKey('tasks.id'), unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    error = Column(Text)
    __table_args__ = (
        UniqueConstraint('channel_id','message_id', name='uq_channel_inbox_message'),
        CheckConstraint("status IN ('pending','processed','ignored','rejected') AND chat_type IN ('private','group')", name='ck_channel_inbox_state'),
        Index('ix_channel_inbox_pending','status','created_at'),
    )


class ChannelOutbox(Base):
    __tablename__ = 'channel_outbox'
    id = Column(String(36), primary_key=True)
    channel_id = Column(String(36), ForeignKey('channel_connections.id'), nullable=False)
    inbox_id = Column(String(36), ForeignKey('channel_inbox.id'), nullable=True)
    schedule_occurrence_id = Column(String(36), ForeignKey('schedule_occurrences.id', name='fk_channel_outbox_schedule_occurrence'), nullable=True)
    task_id = Column(String(36), ForeignKey('tasks.id'))
    user_id = Column(String(36), ForeignKey('users.id'))
    kind = Column(String(32), nullable=False)
    dedupe_key = Column(String(256), nullable=False, unique=True)
    target = Column(JSON, nullable=False)
    text = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default='pending', server_default='pending')
    attempts = Column(Integer, nullable=False, default=0, server_default='0')
    next_attempt_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    lease_owner = Column(String(128))
    lease_until = Column(DateTime(timezone=True))
    platform_message_id = Column(String(256))
    last_error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    sent_at = Column(DateTime(timezone=True))
    reply_revision = Column(Integer, nullable=False, default=1, server_default='1')
    reply_finished = Column(Boolean, nullable=False, default=False, server_default='false')
    reply_state = Column(JSON, nullable=False, default=dict, server_default='{}')
    boundary_key = Column(String(256))
    __table_args__ = (
        CheckConstraint("status IN ('pending','sending','sent','failed','unknown','cancelled') AND attempts>=0", name='ck_channel_outbox_state'),
        UniqueConstraint('schedule_occurrence_id', name='uq_channel_outbox_schedule_occurrence'),
        CheckConstraint('(inbox_id IS NOT NULL AND schedule_occurrence_id IS NULL) OR (inbox_id IS NULL AND schedule_occurrence_id IS NOT NULL)', name='ck_channel_outbox_origin'),
        Index('ix_channel_outbox_due','status','next_attempt_at'),
    )


from sqlalchemy import event, DDL

event.listen(ChannelInbox.__table__, "after_create", DDL("CREATE FUNCTION reject_channel_inbox_mutation() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\nIF TG_OP='DELETE' THEN RAISE EXCEPTION 'channel inbox provenance is immutable'; END IF;\nIF (to_jsonb(NEW)-'status'-'error'-'user_id'-'conversation_id'-'task_id') IS DISTINCT FROM\n   (to_jsonb(OLD)-'status'-'error'-'user_id'-'conversation_id'-'task_id')\n   OR (OLD.user_id IS NOT NULL AND NEW.user_id IS DISTINCT FROM OLD.user_id)\n   OR (OLD.conversation_id IS NOT NULL AND NEW.conversation_id IS DISTINCT FROM OLD.conversation_id)\n   OR (OLD.task_id IS NOT NULL AND NEW.task_id IS DISTINCT FROM OLD.task_id)\nTHEN RAISE EXCEPTION 'channel inbox provenance is immutable'; END IF;\nRETURN NEW;\nEND $$").execute_if(dialect="postgresql"))
event.listen(ChannelInbox.__table__, "after_create", DDL('CREATE TRIGGER channel_inbox_immutable BEFORE UPDATE OR DELETE ON channel_inbox FOR EACH ROW EXECUTE FUNCTION reject_channel_inbox_mutation()').execute_if(dialect="postgresql"))
event.listen(ChannelInbox.__table__, "after_drop", DDL("DROP FUNCTION reject_channel_inbox_mutation()").execute_if(dialect="postgresql"))
event.listen(ChannelInbox.__table__, "after_create", DDL("CREATE TRIGGER channel_inbox_immutable BEFORE UPDATE ON channel_inbox WHEN NEW.id IS NOT OLD.id OR NEW.channel_id IS NOT OLD.channel_id OR NEW.channel_version IS NOT OLD.channel_version OR NEW.message_id IS NOT OLD.message_id OR NEW.sender_id IS NOT OLD.sender_id OR NEW.chat_id IS NOT OLD.chat_id OR NEW.chat_type IS NOT OLD.chat_type OR NEW.text IS NOT OLD.text OR NEW.target IS NOT OLD.target OR NEW.mentioned IS NOT OLD.mentioned OR NEW.unsupported IS NOT OLD.unsupported OR NEW.created_at IS NOT OLD.created_at OR (OLD.user_id IS NOT NULL AND NEW.user_id IS NOT OLD.user_id) OR (OLD.conversation_id IS NOT NULL AND NEW.conversation_id IS NOT OLD.conversation_id) OR (OLD.task_id IS NOT NULL AND NEW.task_id IS NOT OLD.task_id) BEGIN SELECT RAISE(ABORT, 'channel inbox provenance is immutable'); END").execute_if(dialect="sqlite"))
