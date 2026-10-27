"""Start admission persistence on the caller transaction.

Application orders advisory -> Agent -> Conversation -> Task -> Session locks,
interleaving current authorization, idempotency and task preparation. These
operations never commit, open an ORM Session/connection, or alter autoflush policy.
"""
from sqlalchemy import text
from .command_deliveries import stage


async def lock_admission(db):
    return await db.execute(text('SELECT pg_advisory_xact_lock(173542921, 1)'))


async def actor_status(db, actor_user_id):
    return await db.scalar(text('SELECT status FROM users WHERE id=:id'), {'id': actor_user_id})


async def start_for_task(db, task_id):
    return await db.scalar(text("SELECT id FROM runtime_commands WHERE task_id=:id AND kind='StartTask'"),
                                       {'id': task_id})


async def persist_admission(db, command, actor_user_id, digest):
    await db.execute(text('''
        INSERT INTO runtime_task_executions(task_id, session_id)
        VALUES (:tid, :sid)
    '''), {'tid': command.product_task_id, 'sid': command.runtime_session_id})
    await db.execute(text('''
        INSERT INTO runtime_commands(id, session_id, conversation_id, task_id, actor_user_id,
            kind, expected_epoch, status, payload, protocol_version, content_digest,
            reply_id, interaction_id, request_version)
        VALUES (:id,:sid,:cid,:tid,:actor,'StartTask',:epoch,'accepted',CAST(:payload AS jsonb),
            :protocol,:digest,:reply,:interaction,:request)
    '''), {'id': command.command_id, 'sid': command.runtime_session_id, 'cid': command.conversation_id,
          'tid': command.product_task_id, 'actor': actor_user_id, 'epoch': command.expected_epoch,
          'payload': command.canonical_payload().decode(), 'protocol': command.protocol_version,
          'digest': digest, 'reply': command.reply_id, 'interaction': command.interaction_id,
          'request': command.request_version})
    await stage(db, command.command_id)
    await db.execute(text('UPDATE conversations SET active_product_task_id=:tid WHERE id=:id'),
                     {'tid': command.product_task_id, 'id': command.conversation_id})


async def session_identity(db, session_id):
    return await db.scalar(text('SELECT id FROM runtime_sessions WHERE id=:id'),
                                      {'id': session_id})


async def create_session(db, command, storage_node_id):
    await db.execute(text('''
            INSERT INTO runtime_sessions(id, conversation_id, storage_node_id)
            VALUES (:id,:cid,:node)
        '''), {'id': command.runtime_session_id, 'cid': command.conversation_id, 'node': storage_node_id})


async def conversation_agent(db, conversation_id):
    return (await db.execute(text(
        'SELECT agent_id FROM conversations WHERE id=:cid'), {'cid': conversation_id})).mappings().first()


async def lock_agent(db, agent_id):
    return (await db.execute(text('SELECT id, deleted_at FROM agents WHERE id=:id FOR SHARE'),
                             {'id': agent_id})).mappings().first()


async def lock_conversation(db, conversation_id):
    return (await db.execute(text('''
        SELECT id, user_id, agent_id, active_product_task_id, deleted_at FROM conversations
        WHERE id=:id FOR UPDATE
    '''), {'id': conversation_id})).mappings().first()


async def existing_command(db, command_id):
    return (await db.execute(text('''
        SELECT id, task_id, status, content_digest FROM runtime_commands WHERE id=:id
    '''), {'id': command_id})).mappings().first()


async def lock_task(db, task_id):
    return (await db.execute(text('SELECT id, conversation_id, status FROM tasks WHERE id=:id FOR UPDATE'),
                            {'id': task_id})).mappings().first()


async def lock_session(db, conversation_id):
    return (await db.execute(text('SELECT * FROM runtime_sessions WHERE conversation_id=:id AND superseded_at IS NULL FOR UPDATE'),
                               {'id': conversation_id})).mappings().first()
