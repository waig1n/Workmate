"""SQLite schema guards preserve monotonic ledgers and immutable evidence."""
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.exc import DBAPIError


def test_sqlite_node_evidence_and_monotonic_ledger():
    engine=create_engine('sqlite://')
    try:
        with engine.begin() as db:
            from workmate_product.persistence.models import Base
            Base.metadata.create_all(db)
            db.execute(text("INSERT INTO runtime_worker_incarnations(worker_id,payload,digest) VALUES ('w','{}','digest')"))
            db.execute(text("""INSERT INTO runtime_resource_ledgers
                (task_id,execution_epoch,worker_id,session_id,workspace,workspace_digest)
                VALUES ('t',1,'w','s','{}','workspace')"""))
            db.execute(text("INSERT INTO runtime_resource_registrations(task_id,execution_epoch,registration_seq,resource_id,payload,digest) VALUES ('t',1,1,'r','{}','r')"))
            db.execute(text("INSERT INTO runtime_resource_launch_resolutions(task_id,execution_epoch,registration_seq,payload,digest) VALUES ('t',1,1,'{}','resolution')"))
            for query in ('DELETE FROM runtime_resource_launch_resolutions',
                          "UPDATE runtime_resource_launch_resolutions SET digest='changed'"):
                with pytest.raises(DBAPIError,match='immutable'):
                    db.execute(text(query))
            assert db.scalar(text('SELECT created_at FROM runtime_resource_ledgers'))
            db.execute(text('UPDATE runtime_resource_ledgers SET resource_seq=1'))
            db.execute(text("UPDATE runtime_resource_ledgers SET closed=true,boundary='{}',boundary_digest='boundary',ledger_digest='ledger'"))
            for query in ("UPDATE runtime_worker_incarnations SET digest='different'",
                          'UPDATE runtime_resource_ledgers SET closed=false',
                          'DELETE FROM runtime_resource_ledgers'):
                with pytest.raises(DBAPIError,match='immutable|cannot be deleted'):
                    db.execute(text(query))
    finally:
        engine.dispose()
