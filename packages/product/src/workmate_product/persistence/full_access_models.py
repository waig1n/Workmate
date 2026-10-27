"""Immutable permission state separate from original Task execution descriptors."""
from datetime import datetime
from sqlalchemy import BigInteger,JSON,CheckConstraint,DateTime,DDL,ForeignKey,String,UniqueConstraint,event,func
from sqlalchemy.orm import Mapped,mapped_column
from .models import Base


class RuntimeSandboxAttestation(Base):
    __tablename__='runtime_sandbox_attestations'
    task_id: Mapped[str]=mapped_column(ForeignKey('tasks.id'),primary_key=True)
    session_id: Mapped[str]=mapped_column(ForeignKey('runtime_sessions.id'))
    worker_id: Mapped[str]=mapped_column(String(128))
    execution_epoch: Mapped[int]=mapped_column(BigInteger,primary_key=True)
    workspace_id: Mapped[str]=mapped_column(String(128))
    isolation: Mapped[str]=mapped_column(String(32),server_default='bubblewrap')
    containment: Mapped[str]=mapped_column(String(32),server_default='pid_namespace')
    instance_id: Mapped[str]=mapped_column(String(256),server_default='')
    evidence: Mapped[dict]=mapped_column(JSON,server_default='{}')
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
    __table_args__=(CheckConstraint('execution_epoch>0',name='ck_runtime_sandbox_epoch'),
        CheckConstraint("(isolation='bubblewrap' AND containment='pid_namespace') OR "
            "(isolation IN ('docker','kubernetes','e2b','daytona','opensandbox') "
            "AND containment='dedicated_uid' AND length(trim(instance_id))>0)",name='ck_runtime_sandbox_proof_kind'))


class RuntimePermissionUpgrade(Base):
    __tablename__='runtime_permission_upgrades'
    task_id: Mapped[str]=mapped_column(ForeignKey('tasks.id'))
    session_id: Mapped[str]=mapped_column(ForeignKey('runtime_sessions.id'))
    command_id: Mapped[str]=mapped_column(ForeignKey('runtime_commands.id'),primary_key=True)
    permission_version: Mapped[str]=mapped_column(String(128))
    state_version: Mapped[int]=mapped_column(BigInteger)
    actor_user_id: Mapped[str]=mapped_column(ForeignKey('users.id'))
    worker_id: Mapped[str]=mapped_column(String(128))
    execution_epoch: Mapped[int]=mapped_column(BigInteger)
    initial_profile: Mapped[str]=mapped_column(String(32))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
    __table_args__=(CheckConstraint('execution_epoch>0',name='ck_runtime_permission_upgrade_epoch'),
        CheckConstraint("initial_profile IN ('standard','strict','read_only','full_access')",name='ck_runtime_permission_upgrade_profile'),
        CheckConstraint('state_version>0',name='ck_runtime_permission_upgrade_state'),
        UniqueConstraint('task_id','permission_version',name='uq_runtime_permission_upgrade_version'),
        UniqueConstraint('task_id','state_version',name='uq_runtime_permission_upgrade_boundary'))


for model in (RuntimeSandboxAttestation,RuntimePermissionUpgrade):
    table=model.__tablename__
    for operation in ('UPDATE','DELETE'):
        event.listen(model.__table__,'after_create',DDL(
            f'CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} '
            "BEGIN SELECT RAISE(ABORT, 'runtime permission records are immutable'); END").execute_if(dialect='sqlite'))
    event.listen(model.__table__,'after_create',DDL(
        f"CREATE FUNCTION reject_{table}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'runtime permission records are immutable'; END $$").execute_if(dialect='postgresql'))
    event.listen(model.__table__,'after_create',DDL(
        f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} '
        f'FOR EACH ROW EXECUTE FUNCTION reject_{table}_mutation()').execute_if(dialect='postgresql'))
    event.listen(model.__table__,'after_drop',DDL(f'DROP FUNCTION reject_{table}_mutation()').execute_if(dialect='postgresql'))
