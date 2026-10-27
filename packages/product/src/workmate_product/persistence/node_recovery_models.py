"""Durable node observations are evidence, never a grant to resume execution."""
from datetime import datetime
from sqlalchemy import BigInteger,Boolean,CheckConstraint,DateTime,DDL,ForeignKey,ForeignKeyConstraint,String,UniqueConstraint,event,func
from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from .models import Base,_RUNTIME_JSON


class _ObservationTimestamp(FunctionElement):
    type=DateTime(timezone=True)
    inherit_cache=True


@compiles(_ObservationTimestamp)
def _default_timestamp(element,compiler,**kwargs):
    return "CURRENT_TIMESTAMP"


@compiles(_ObservationTimestamp,"postgresql")
def _postgres_timestamp(element,compiler,**kwargs):
    return "clock_timestamp()"


class RuntimeWorkerIncarnation(Base):
    __tablename__='runtime_worker_incarnations'
    worker_id:Mapped[str]=mapped_column(String(128),primary_key=True)
    payload:Mapped[dict]=mapped_column(_RUNTIME_JSON)
    digest:Mapped[str]=mapped_column(String(64))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=_ObservationTimestamp())


class RuntimeResourceLedger(Base):
    __tablename__='runtime_resource_ledgers'
    task_id:Mapped[str]=mapped_column(ForeignKey('tasks.id'),primary_key=True)
    execution_epoch:Mapped[int]=mapped_column(BigInteger,primary_key=True)
    worker_id:Mapped[str]=mapped_column(ForeignKey('runtime_worker_incarnations.worker_id'))
    session_id:Mapped[str]=mapped_column(ForeignKey('runtime_sessions.id'))
    workspace:Mapped[dict]=mapped_column(_RUNTIME_JSON)
    workspace_digest:Mapped[str]=mapped_column(String(64))
    resource_seq:Mapped[int]=mapped_column(BigInteger,server_default='0')
    closed:Mapped[bool]=mapped_column(Boolean,server_default='false')
    boundary:Mapped[dict|None]=mapped_column(_RUNTIME_JSON,nullable=True)
    boundary_digest:Mapped[str|None]=mapped_column(String(64),nullable=True)
    ledger_digest:Mapped[str|None]=mapped_column(String(64),nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=_ObservationTimestamp())
    __table_args__=(CheckConstraint('execution_epoch>0 AND resource_seq>=0',name='ck_resource_ledger_versions'),
        CheckConstraint('(NOT closed AND boundary IS NULL AND boundary_digest IS NULL AND ledger_digest IS NULL) OR '
            '(closed AND resource_seq>0 AND boundary IS NOT NULL AND boundary_digest IS NOT NULL AND ledger_digest IS NOT NULL)',
            name='ck_resource_ledger_closure'))


class RuntimeResourceRegistration(Base):
    __tablename__='runtime_resource_registrations'
    task_id:Mapped[str]=mapped_column(String(36),primary_key=True)
    execution_epoch:Mapped[int]=mapped_column(BigInteger,primary_key=True)
    registration_seq:Mapped[int]=mapped_column(BigInteger,primary_key=True)
    resource_id:Mapped[str]=mapped_column(String(128))
    payload:Mapped[dict]=mapped_column(_RUNTIME_JSON)
    digest:Mapped[str]=mapped_column(String(64))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=_ObservationTimestamp())
    __table_args__=(ForeignKeyConstraint(['task_id','execution_epoch'],['runtime_resource_ledgers.task_id','runtime_resource_ledgers.execution_epoch']),
        UniqueConstraint('task_id','execution_epoch','resource_id',name='uq_resource_registration_identity'),
        CheckConstraint('registration_seq>0',name='ck_resource_registration_sequence'))


class RuntimeResourceLaunchResolution(Base):
    __tablename__='runtime_resource_launch_resolutions'
    task_id:Mapped[str]=mapped_column(String(36),primary_key=True)
    execution_epoch:Mapped[int]=mapped_column(BigInteger,primary_key=True)
    registration_seq:Mapped[int]=mapped_column(BigInteger,primary_key=True)
    payload:Mapped[dict]=mapped_column(_RUNTIME_JSON)
    digest:Mapped[str]=mapped_column(String(64))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=_ObservationTimestamp())
    __table_args__=(ForeignKeyConstraint(['task_id','execution_epoch','registration_seq'],
        ['runtime_resource_registrations.task_id','runtime_resource_registrations.execution_epoch',
         'runtime_resource_registrations.registration_seq']),)


class RuntimeNodeQuiescenceEvidence(Base):
    __tablename__='runtime_node_quiescence_evidence'
    evidence_id:Mapped[str]=mapped_column(String(128),primary_key=True)
    task_id:Mapped[str]=mapped_column(String(36))
    execution_epoch:Mapped[int]=mapped_column(BigInteger)
    payload:Mapped[dict]=mapped_column(_RUNTIME_JSON)
    digest:Mapped[str]=mapped_column(String(64))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=_ObservationTimestamp())
    __table_args__=(ForeignKeyConstraint(['task_id','execution_epoch'],['runtime_resource_ledgers.task_id','runtime_resource_ledgers.execution_epoch']),
        UniqueConstraint('task_id','execution_epoch',name='uq_node_quiescence_execution'))


for model in (RuntimeWorkerIncarnation,RuntimeResourceRegistration,RuntimeNodeQuiescenceEvidence,RuntimeResourceLaunchResolution):
    table=model.__tablename__
    for operation in ('UPDATE','DELETE'):
        event.listen(model.__table__,'after_create',DDL(f'CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} '
            "BEGIN SELECT RAISE(ABORT, 'node evidence records are immutable'); END").execute_if(dialect='sqlite'))
    event.listen(model.__table__,'after_create',DDL(f'CREATE FUNCTION reject_{table}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ '
        "BEGIN RAISE EXCEPTION 'node evidence records are immutable'; END $$").execute_if(dialect='postgresql'))
    event.listen(model.__table__,'after_create',DDL(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} '
        f'FOR EACH ROW EXECUTE FUNCTION reject_{table}_mutation()').execute_if(dialect='postgresql'))
    event.listen(model.__table__,'after_drop',DDL(f'DROP FUNCTION reject_{table}_mutation()').execute_if(dialect='postgresql'))

# Identity is permanent; only contiguous registration or one atomic closure may
# advance an open ledger. Neither an application bug nor a retry may reopen it.
ledger=RuntimeResourceLedger.__table__
_identity_fields=('task_id','execution_epoch','worker_id','session_id','workspace','workspace_digest','created_at')
_pg_changed=' OR '.join(f'OLD.{field} IS DISTINCT FROM NEW.{field}' for field in _identity_fields)
event.listen(ledger,'after_create',DDL(
    'CREATE FUNCTION protect_runtime_resource_ledger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN '
    "IF TG_OP='DELETE' THEN RAISE EXCEPTION 'resource ledgers cannot be deleted'; END IF; "
    f'IF OLD.closed OR {_pg_changed} OR '
    '(NOT NEW.closed AND NEW.resource_seq<>OLD.resource_seq+1) OR '
    '(NEW.closed AND NEW.resource_seq<>OLD.resource_seq) THEN '
    "RAISE EXCEPTION 'resource ledger identity and closure are immutable'; END IF; RETURN NEW; END $$").execute_if(dialect='postgresql'))
event.listen(ledger,'after_create',DDL('CREATE TRIGGER runtime_resource_ledger_guard BEFORE UPDATE OR DELETE ON runtime_resource_ledgers '
    'FOR EACH ROW EXECUTE FUNCTION protect_runtime_resource_ledger()').execute_if(dialect='postgresql'))
event.listen(ledger,'after_drop',DDL('DROP FUNCTION protect_runtime_resource_ledger()').execute_if(dialect='postgresql'))
_sqlite_changed=' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in _identity_fields)
event.listen(ledger,'after_create',DDL('CREATE TRIGGER runtime_resource_ledger_guard BEFORE UPDATE ON runtime_resource_ledgers '
    f'WHEN OLD.closed OR {_sqlite_changed} OR (NOT NEW.closed AND NEW.resource_seq<>OLD.resource_seq+1) '
    'OR (NEW.closed AND NEW.resource_seq<>OLD.resource_seq) '
    "BEGIN SELECT RAISE(ABORT, 'resource ledger identity and closure are immutable'); END").execute_if(dialect='sqlite'))
event.listen(ledger,'after_create',DDL('CREATE TRIGGER runtime_resource_ledger_no_delete BEFORE DELETE ON runtime_resource_ledgers '
    "BEGIN SELECT RAISE(ABORT, 'resource ledgers cannot be deleted'); END").execute_if(dialect='sqlite'))
