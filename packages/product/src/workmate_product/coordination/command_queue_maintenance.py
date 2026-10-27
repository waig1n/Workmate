"""Retire old-incarnation command notifications using durable delivery facts.

No resources are touched. Redis health absence only enables inspection; PG owner
leases and residence state still forbid replay or transfer of unknown execution.
"""
from sqlalchemy import text

from .command_queue import CommandEnvelope, _text
from .directed_queue_maintenance import DirectedQueueMaintenance
from ..persistence.repositories import command_deliveries as repository


class CommandQueueMaintenance(DirectedQueueMaintenance):
    consumer = 'gateway-command-maintenance'

    def __init__(self, sessions, queue, registry, *, max_generations=20):
        super().__init__(sessions, queue, registry, suffixes=('execute','control'))
        self.max_generations = max_generations

    async def _disposition(self, route, message_id, fields):
        stream = self.queue.stream_key(route)
        try:
            normalized = {_text(key): value for key, value in fields.items()}
            if set(normalized) != {'payload'}:
                raise ValueError('Unexpected command envelope fields')
            envelope = CommandEnvelope.model_validate_json(normalized['payload'])
        except (ValueError, TypeError, UnicodeError):
            async with self.sessions.begin() as db:
                await repository.quarantine_message(db,stream,message_id,'invalid_envelope')
            return True
        if await self.registry.worker(route.worker_id, allow_draining=True):
            return False
        async with self.sessions.begin() as db:
            try:
                # Delivery routes are immutable per generation. Verify the
                # actual stream channel before classify can settle or reroute.
                channel = await db.scalar(text('SELECT channel FROM command_deliveries '
                    'WHERE command_id=:id AND delivery_generation=:generation'),
                    {'id':envelope.command_id,'generation':envelope.delivery_generation})
                if (envelope.target_incarnation != route.worker_id or
                        channel != ('control' if route.control else 'execute')):
                    raise ValueError('Invalid directed channel')
                result = await repository.classify(db,envelope,worker_id=route.worker_id,
                    node_id=envelope.storage_node_id,max_generations=self.max_generations)
                if result.action == 'wait' and await repository.quarantine_abandoned(
                        db,envelope.command_id,envelope.delivery_generation):
                    result = await repository.classify(db,envelope,worker_id=route.worker_id,
                        node_id=envelope.storage_node_id,max_generations=self.max_generations)
            except ValueError:
                await repository.quarantine_message(db,stream,message_id,'invalid_delivery',envelope.command_id)
                return True
            if result.action == 'ack':
                return True  # Context commits terminal disposition/replacement before caller ACK.
            await repository.quarantine_message(db,stream,message_id,
                'delivery_holder_unavailable',envelope.command_id)
            return False
