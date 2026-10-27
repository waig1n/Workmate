"""Channel replies retain transport state and initialized defaults."""
import json


from test_channel_schema import migrated, seed  # noqa: F401


async def insert_delivery(db, id, kind='accepted', status='pending'):
    await db.execute('''INSERT INTO channel_outbox
        (id,channel_id,inbox_id,task_id,user_id,kind,dedupe_key,target,text,status)
        VALUES($1,'ch','i','t','u',$2,$1,'{}','Example reply',$3)''', id, kind, status)


async def test_initial_schema_supplies_reply_defaults(migrated):
    db, _, apply = migrated
    await seed(db)
    for status in ('pending', 'sending', 'sent', 'failed', 'unknown', 'cancelled'):
        await insert_delivery(db, status, status=status)
    await insert_delivery(db, 'result', kind='result')

    rows = await db.fetch('SELECT * FROM channel_outbox ORDER BY id')
    assert len(rows) == 7
    for row in rows:
        expected = row['id']
        assert row['status'] == ('pending' if row['id'] == 'result' else expected)
        assert row['text'] == 'Example reply'
        assert row['reply_revision'] == 1
        assert row['reply_finished'] is False
        assert json.loads(row['reply_state']) == {}
        assert row['boundary_key'] is None

    # Reply inserts use the same defaults.
    await insert_delivery(db, 'new', kind='reply')
    assert await db.fetchval("SELECT reply_revision FROM channel_outbox WHERE id='new'") == 1


async def test_reply_state_persists(migrated):
    db, _, apply = migrated
    await seed(db)
    await insert_delivery(db, 'reply', kind='reply', status='sent')
    await db.execute('''UPDATE channel_outbox SET reply_revision=3,reply_finished=true,
        reply_state='{"message_id":"remote-reply"}',boundary_key='completed'
        WHERE id='reply' ''')
    row = await db.fetchrow("SELECT * FROM channel_outbox WHERE id='reply'")
    assert row['reply_revision'] == 3
    assert row['reply_finished'] is True
    assert json.loads(row['reply_state']) == {'message_id': 'remote-reply'}
    assert row['boundary_key'] == 'completed'
