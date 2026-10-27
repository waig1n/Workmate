"""Initial PostgreSQL schema enforces schedule admission and immutable occurrences."""

import pytest


from test_channel_schema import migrated as migration_database


async def seed(admin):
    await admin.execute('''INSERT INTO schedules(id,owner_user_id,agent_id,name,prompt,timezone,rule)
        VALUES ('s','u','a','Report','Prepare report','Asia/Shanghai','{"kind":"daily","time":"09:00"}');
        INSERT INTO schedule_occurrences(id,schedule_id,schedule_version,scheduled_for,triggered_at,outcome,snapshot)
        VALUES ('o','s',1,now(),now(),'task_created','{"owner_user_id":"u","approval_profile":"unattended"}');''')


async def test_initial_schema_enforces_task_origin(migration_database):
    admin, _, _ = migration_database
    assert await admin.fetchval("SELECT origin FROM tasks WHERE id='old'") == 'user'
    with pytest.raises(Exception, match='ck_task_schedule_origin'):
        await admin.execute("INSERT INTO tasks(conversation_id,title,id,approval_profile,origin,schedule_occurrence_id) VALUES ('c','Example','bad','standard','schedule',NULL)")
    await seed(admin)
    await admin.execute("INSERT INTO tasks(conversation_id,title,id,approval_profile,origin,schedule_occurrence_id) VALUES ('c','Example','t','unattended','schedule','o')")
    await admin.execute("UPDATE schedule_occurrences SET task_id='t',conversation_id='c' WHERE id='o'")
    assert await admin.fetchval("SELECT task_id FROM schedule_occurrences WHERE id='o'") == 't'


@pytest.mark.parametrize('statement', [
    "UPDATE schedule_occurrences SET snapshot='{}' WHERE id='o'",
    "UPDATE schedule_occurrences SET schedule_version=2 WHERE id='o'",
    "UPDATE schedule_occurrences SET scheduled_for=scheduled_for+interval '1 day' WHERE id='o'",
    "UPDATE schedule_occurrences SET outcome='missed' WHERE id='o'",
    "UPDATE schedule_occurrences SET task_id=NULL WHERE id='o'",
    "UPDATE schedule_occurrences SET conversation_id=NULL WHERE id='o'",
    "DELETE FROM schedule_occurrences WHERE id='o'",
])
async def test_occurrence_authority_and_bound_identity_are_immutable(migration_database, statement):
    admin, _, _ = migration_database
    await seed(admin)
    await admin.execute("INSERT INTO tasks(conversation_id,title,id,approval_profile,origin,schedule_occurrence_id) VALUES ('c','Example','t','unattended','schedule','o')")
    await admin.execute("UPDATE schedule_occurrences SET task_id='t',conversation_id='c' WHERE id='o'")
    with pytest.raises(Exception, match='immutable'):
        await admin.execute(statement)
