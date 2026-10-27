"""Validate Stop authority without revoking an already accepted cancellation."""
import json

from workmate_contracts.controls import StopPayload


async def admitted_stop_actor(connection, command, conversation):
    try:
        payload = StopPayload.model_validate(json.loads(command['payload']))
    except (ValueError, TypeError):
        return False
    if payload.reason == 'user_requested':
        return command['actor_user_id'] == conversation['user_id']
    # Only Product admission writes this audit fact, in the same transaction as
    # the administrator/policy command. A reason string alone cannot widen ownership.
    # Role/status changes after admission must not keep unsafe execution alive.
    return bool(await connection.fetchval('''SELECT 1 FROM task_events
        WHERE task_id=$1 AND conversation_id=$2 AND event_type='task_cancel_requested'
          AND payload->>'commandId'=$3 AND payload->>'actorUserId'=$4
          AND payload->>'targetUserId'=$5 AND payload->>'reason'=$6
          AND ($6!='approval_expired' OR (payload->>'initiatedBy'='approval_expiry_policy'
            AND payload->>'requestVersion' IS NOT NULL))
        LIMIT 1''', command['task_id'], conversation['id'], command['id'],
        command['actor_user_id'], conversation['user_id'], payload.reason))
