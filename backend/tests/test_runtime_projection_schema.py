"""Projection schema enforces cursors and unique event identities."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def connection(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'projection.db'))
    with engine.begin() as db:
        db.execute(text('PRAGMA foreign_keys=ON'))
        for statement in [
            'CREATE TABLE conversations (id VARCHAR(36) PRIMARY KEY, title TEXT)',
            'CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY, conversation_id VARCHAR(36) REFERENCES conversations(id))',
            'CREATE TABLE runtime_sessions (id VARCHAR(128) PRIMARY KEY, conversation_id VARCHAR(36) REFERENCES conversations(id))',
            'CREATE TABLE runtime_events (id VARCHAR(128) PRIMARY KEY, session_id VARCHAR(128) REFERENCES runtime_sessions(id), runtime_seq BIGINT)',
            "INSERT INTO conversations VALUES ('conv','existing'),('other','other')",
            "INSERT INTO tasks VALUES ('task','conv')",
            "INSERT INTO runtime_sessions VALUES ('session','conv')",
            "INSERT INTO runtime_events VALUES ('source','session',1)",
        ]:
            db.execute(text(statement))
        db.execute(text('ALTER TABLE conversations ADD COLUMN product_seq BIGINT NOT NULL DEFAULT 0 CHECK(product_seq >= 0)'))
        from workmate_product.persistence.models import Base
        Base.metadata.create_all(db,tables=[Base.metadata.tables[name] for name in
            ('runtime_projection_cursors','task_projection_states','product_events')])
        yield db
    engine.dispose()


def test_projection_defaults_and_cursor_constraints(connection):
    db = connection
    assert db.execute(text("SELECT title,product_seq FROM conversations WHERE id='conv'")).one() == ('existing',0)
    assert db.scalar(text('SELECT count(*) FROM runtime_events')) == 1
    assert db.scalar(text('SELECT count(*) FROM runtime_projection_cursors')) == 0
    assert db.scalar(text('SELECT count(*) FROM product_events')) == 0
    assert db.scalar(text('SELECT count(*) FROM task_projection_states')) == 0
    db.execute(text("INSERT INTO task_projection_states(task_id,session_id) VALUES ('task','session')"))
    assert db.execute(text('SELECT format_version,last_runtime_seq,state FROM task_projection_states')).one() == (1,0,'{}')
    for sql in ['UPDATE task_projection_states SET format_version=2', 'UPDATE task_projection_states SET last_runtime_seq=-1']:
        with pytest.raises(IntegrityError):
            db.execute(text(sql))
    db.execute(text("INSERT INTO runtime_projection_cursors(conversation_id,session_id) VALUES ('conv','session')"))
    assert db.execute(text('SELECT last_runtime_seq,version FROM runtime_projection_cursors')).one() == (0,0)
    with pytest.raises(IntegrityError):
        db.execute(text("INSERT INTO runtime_projection_cursors(conversation_id,session_id) VALUES ('other','session')"))
    for statement in [
        "UPDATE conversations SET product_seq=-1 WHERE id='conv'",
        'UPDATE runtime_projection_cursors SET last_runtime_seq=-1',
        'UPDATE runtime_projection_cursors SET version=-1',
    ]:
        with pytest.raises(IntegrityError):
            db.execute(text(statement))


def test_projection_unique_keys_prevent_duplicate_runtime_and_business_items(connection):
    db = connection
    insert = text("""INSERT INTO product_events(id,conversation_id,product_seq,task_id,
        source_runtime_event_id,projection_item_key,business_operation_key,event_type,occurred_at,payload)
        VALUES (:id,'conv',:seq,'task',:source,:item,:operation,'TEXT_MESSAGE_CONTENT',CURRENT_TIMESTAMP,'{}')""")
    def row(id, seq, source=None, item='text', operation=None):
        return {'id':id,'seq':seq,'source':source,'item':item,'operation':operation}
    db.execute(insert,row('event',1,'source'))
    for duplicate in [row('duplicate-seq',1,item='other'), row('duplicate-source',2,'source'), row('invalid-seq',0)]:
        with pytest.raises(IntegrityError):
            db.execute(insert,duplicate)
    # A single runtime event may produce distinct projection items.
    db.execute(insert,row('other-item',2,'source','tool'))
    db.execute(insert,row('business',3,operation='approval'))
    with pytest.raises(IntegrityError):
        db.execute(insert,row('duplicate-business',4,operation='approval'))
    db.execute(insert,row('business-other-item',4,item='status',operation='approval'))
    with pytest.raises(IntegrityError):
        db.execute(insert,row('missing-source',5,'missing-source'))


def test_projection_models_registered_and_columns_match(connection):
    from workmate_product.persistence.models import (
        Base,
        ProductEvent,
        RuntimeProjectionCursor,
        TaskProjectionState,
    )
    from sqlalchemy.orm import configure_mappers
    configure_mappers()
    for model in (ProductEvent,RuntimeProjectionCursor,TaskProjectionState):
        assert Base.metadata.tables[model.__tablename__] is model.__table__
        assert set(model.__table__.c.keys()) == {
            column['name'] for column in inspect(connection).get_columns(model.__tablename__)}
