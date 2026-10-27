"""Public approval cards and replay tombstones share one decision interpretation."""
from workmate_product.approval import approval_request_version
from workmate_product.persistence.repositories.conversation_display import approval_events

from ..api.schemas import PendingConfirmOut
from ..approval import permission_mode_for


def present_approval_history(task, events):
    requests, decided, closed = {}, set(), set()
    for event in events:
        payload = event.payload or {}
        if event.event_type == 'hitl_request':
            calls = payload.get('toolCalls')
            if not isinstance(calls, list) or not calls:
                continue
            reply = payload.get('replyId') or task.run_id or ''
            version = payload.get('decisionVersion') or approval_request_version(reply, calls)
            requests[version] = (reply, calls)
        elif event.event_type == 'hitl_decision':
            decided.update(item['toolCallId'] for item in payload.get('decisions', [])
                if isinstance(item, dict) and isinstance(item.get('toolCallId'), str))
        elif isinstance(payload.get('decisionVersion'), str):
            closed.add(payload['decisionVersion'])
    decisions = {version: sorted({call.get('id') for call in calls} & decided)
                 for version, (_, calls) in requests.items()}
    for version, (_, calls) in requests.items():
        if all(call.get('id') in decided for call in calls):
            closed.add(version)
    pending = None
    if task.status == 'awaiting_permission':
        for version in reversed(requests):
            reply, original = requests[version]
            calls = [call for call in original if call.get('id') not in decided]
            if version in closed or not calls or reply != task.run_id:
                continue
            # Approval inputs are intentionally public to the approver. Other
            # arbitrary audit payload fields never become part of a reset.
            calls = [{key: call[key] for key in ('id', 'name', 'input', 'state', 'suggested_rules')
                      if key in call} for call in calls]
            pending = PendingConfirmOut(toolCalls=calls, decisionVersion=version, taskId=task.id,
                approvalProfile=task.approval_profile,
                permissionMode=permission_mode_for(task.approval_profile)).model_dump(mode='json')
            break
    return pending, decisions, sorted(closed)


async def pending_confirm(db, task):
    return present_approval_history(task, await approval_events(db, task.id))[0]
