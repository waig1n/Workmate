"""Only a committed, exact display cut may certify a public execution boundary."""
from workmate_contracts.realtime import MessageSnapshotReference, message_id
from workmate_product.persistence.repositories.message_snapshots import from_message
from workmate_product.projections.store import ProjectionConflict

from .durable_projection import ProjectionDeferred


def display_evidence(event, message, *, phases):
    if 'display_empty' in event.payload:
        if (event.payload['display_empty'] is not True or 'display_snapshot' in event.payload
                or message is not None):
            raise ProjectionConflict('Empty display evidence conflicts with stored content')
        return {'display_empty': True}
    if 'display_snapshot' not in event.payload:
        raise ProjectionConflict('Execution boundary requires explicit display evidence')
    try:
        reference = MessageSnapshotReference.model_validate(event.payload['display_snapshot'])
    except ValueError as error:
        raise ProjectionConflict('Invalid display snapshot reference') from error
    if (reference.message_id != message_id(event.product_task_id, event.reply_id)
            or reference.execution_epoch != event.execution_epoch):
        raise ProjectionConflict('The display snapshot reference belongs to another execution')
    if message is None:
        raise ProjectionDeferred('The referenced display snapshot has not committed')
    if (message.role != 'assistant' or message.snapshot_version == 0
            or message.task_id != event.product_task_id or message.conversation_id != event.conversation_id
            or message.snapshot_reply_id != event.reply_id
            or message.snapshot_execution_epoch != event.execution_epoch):
        raise ProjectionConflict('The display message belongs to another execution')
    if message.snapshot_phase not in phases:
        raise ProjectionDeferred('The referenced display phase has not committed')
    if MessageSnapshotReference.from_snapshot(from_message(message)) != reference:
        raise ProjectionConflict('The display reference does not match committed content')
    return {'display_snapshot': reference.model_dump(mode='json')}
