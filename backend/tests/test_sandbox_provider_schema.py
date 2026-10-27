"""Sandbox provider proofs enforce defaults, evidence and immutable identity."""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


def test_provider_proof_defaults_and_immutability():
    engine=create_engine('sqlite://')
    with engine.begin() as db:
        from workmate_product.persistence.models import Base
        Base.metadata.create_all(db)
        db.execute(text("INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id) VALUES ('old','s','w',1,'ws')"))
        row=db.execute(text('SELECT * FROM runtime_sandbox_attestations')).mappings().one()
        assert row['isolation']=='bubblewrap' and row['evidence']=='{}' and row['instance_id']==''
        for query in ('DELETE FROM runtime_sandbox_attestations',"UPDATE runtime_sandbox_attestations SET instance_id='tampered'"):
            with pytest.raises(DBAPIError,match='immutable'):
                db.execute(text(query))
        base="INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id,isolation,containment,instance_id,evidence) VALUES (:task,'s','w',1,'ws',:provider,'dedicated_uid',:instance,:evidence)"
        for provider in ('docker','kubernetes','e2b','daytona','opensandbox'):
            db.execute(text(base),{'task':provider,'provider':provider,'instance':'actual','evidence':'{"uid":60000,"no_new_privs":true}'})
        with pytest.raises(DBAPIError):
            db.execute(text(base),{'task':'invalid','provider':'docker','instance':'','evidence':'{}'})
    engine.dispose()
