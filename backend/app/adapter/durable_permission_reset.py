"""Record Task-scoped Full Access expiration in the terminal Product transaction."""
import json
import uuid
from sqlalchemy import text


async def record_permission_reset(db,event,task):
    from .durable_projection import _audit
    upgrade=(await db.execute(text('''SELECT permission_version FROM runtime_permission_upgrades
        WHERE task_id=:id ORDER BY state_version DESC LIMIT 1'''),{'id':task.id})).mappings().first()
    if task.approval_profile!='full_access' and upgrade is None:
        return
    actor=await db.scalar(text('SELECT user_id FROM conversations WHERE id=:id'),{'id':event.conversation_id})
    await _audit(db,event,'permission_mode_changed',{'from':'bypass','to':'default',
        'reason':'task_ended','actorUserId':actor,'decisionVersion':upgrade['permission_version'] if upgrade else None})
    await db.execute(text('''INSERT INTO security_audit_events(id,event_type,actor_user_id,metadata)
        VALUES (:id,'task.full_access_reset',:actor,CAST(:metadata AS json))'''),
        {'id':str(uuid.uuid4()),'actor':actor,'metadata':json.dumps({'task_id':task.id,
            'conversation_id':event.conversation_id,'reason':'task_ended'})})
