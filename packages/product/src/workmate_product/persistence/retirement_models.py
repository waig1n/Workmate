"""Durable logical deletion request and trusted Worker completion receipt."""
from datetime import datetime
from sqlalchemy import String, ForeignKey, DateTime, BigInteger, CheckConstraint, Index, func
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base


class ConversationRetirement(Base):
    __tablename__ = 'conversation_retirements'
    conversation_id: Mapped[str] = mapped_column(ForeignKey('conversations.id'), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey('runtime_sessions.id'), unique=True)
    storage_node_id: Mapped[str | None] = mapped_column(String(128))
    execution_epoch: Mapped[int] = mapped_column(BigInteger, server_default='0')
    requested_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[str | None] = mapped_column(String(128))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_by: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        Index('ix_retirement_purge_pending', 'purged_at', 'storage_node_id', 'conversation_id'),
        Index('ix_retirement_pending_node', 'storage_node_id', 'completed_at', 'conversation_id'),
        CheckConstraint('execution_epoch >= 0', name='ck_retirement_epoch'),
        CheckConstraint('(session_id IS NULL) = (storage_node_id IS NULL)', name='ck_retirement_storage'),
    )

from sqlalchemy import event, DDL
from .models import Conversation
event.listen(ConversationRetirement.__table__, 'after_create', DDL("""CREATE FUNCTION protect_conversation_retirement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'retirement requests are retained'; END IF;
 IF (to_jsonb(NEW) - 'completed_at' - 'completed_by') IS DISTINCT FROM
    (to_jsonb(OLD) - 'completed_at' - 'completed_by') OR
    (OLD.completed_at IS NOT NULL AND NEW IS DISTINCT FROM OLD) OR
    (NEW.completed_at IS NULL) <> (NEW.completed_by IS NULL)
 THEN RAISE EXCEPTION 'retirement request identity and completion are immutable'; END IF;
 RETURN NEW;
END $$""").execute_if(dialect='postgresql'))
event.listen(ConversationRetirement.__table__, 'after_create', DDL('CREATE TRIGGER protect_conversation_retirement BEFORE UPDATE OR DELETE ON conversation_retirements FOR EACH ROW EXECUTE FUNCTION protect_conversation_retirement()').execute_if(dialect='postgresql'))
event.listen(ConversationRetirement.__table__, 'after_drop', DDL('DROP FUNCTION protect_conversation_retirement()').execute_if(dialect='postgresql'))
event.listen(Conversation.__table__, 'after_create', DDL("""CREATE FUNCTION protect_conversation_tombstone() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
 THEN RAISE EXCEPTION 'conversation retirement cannot be reversed'; END IF;
 RETURN NEW;
END $$""").execute_if(dialect='postgresql'))
event.listen(Conversation.__table__, 'after_create', DDL('CREATE TRIGGER protect_conversation_tombstone BEFORE UPDATE ON conversations FOR EACH ROW EXECUTE FUNCTION protect_conversation_tombstone()').execute_if(dialect='postgresql'))
event.listen(Conversation.__table__, 'after_drop', DDL('DROP FUNCTION protect_conversation_tombstone()').execute_if(dialect='postgresql'))


@event.listens_for(Base.metadata, 'after_create')
def _install_purge_guards(metadata, connection, **kwargs):
    from .purge_schema import install_purge_guards
    from sqlalchemy import inspect
    if 'conversation_retirements' in inspect(connection).get_table_names():
        install_purge_guards(connection)
