"""Immutable external operation receipts, independent from browser approval."""
from datetime import datetime
from sqlalchemy import BigInteger,CheckConstraint,DateTime,DDL,ForeignKey,String,UniqueConstraint,event,func
from sqlalchemy.orm import Mapped,mapped_column
from .models import Base,_RUNTIME_JSON


class RuntimeExternalReceipt(Base):
    __tablename__='runtime_external_receipts'
    receipt_id: Mapped[str]=mapped_column(String(128),primary_key=True)
    task_id: Mapped[str]=mapped_column(ForeignKey('tasks.id'))
    session_id: Mapped[str]=mapped_column(ForeignKey('runtime_sessions.id'))
    reply_id: Mapped[str]=mapped_column(String(128))
    execution_epoch: Mapped[int]=mapped_column(BigInteger)
    tool_call_id: Mapped[str]=mapped_column(String(128))
    producer_id: Mapped[str]=mapped_column(String(128))
    job_id: Mapped[str]=mapped_column(String(128))
    payload: Mapped[dict]=mapped_column(_RUNTIME_JSON)
    digest: Mapped[str]=mapped_column(String(64))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
    __table_args__=(UniqueConstraint('task_id','tool_call_id',name='uq_external_receipt_tool'),
        UniqueConstraint('producer_id','job_id',name='uq_external_receipt_job'),
        CheckConstraint('execution_epoch>0',name='ck_external_receipt_epoch'),
        CheckConstraint('length(digest)=64',name='ck_external_receipt_digest'))


for operation in ('UPDATE','DELETE'):
    event.listen(RuntimeExternalReceipt.__table__,'after_create',DDL(
        f'CREATE TRIGGER external_receipt_no_{operation.lower()} BEFORE {operation} ON runtime_external_receipts '
        "BEGIN SELECT RAISE(ABORT, 'external receipts are immutable'); END").execute_if(dialect='sqlite'))
event.listen(RuntimeExternalReceipt.__table__,'after_create',DDL(
    "CREATE FUNCTION reject_external_receipt_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "BEGIN RAISE EXCEPTION 'external receipts are immutable'; END $$").execute_if(dialect='postgresql'))
event.listen(RuntimeExternalReceipt.__table__,'after_create',DDL(
    'CREATE TRIGGER external_receipt_immutable BEFORE UPDATE OR DELETE ON runtime_external_receipts '
    'FOR EACH ROW EXECUTE FUNCTION reject_external_receipt_mutation()').execute_if(dialect='postgresql'))
event.listen(RuntimeExternalReceipt.__table__,'after_drop',DDL('DROP FUNCTION reject_external_receipt_mutation()').execute_if(dialect='postgresql'))
