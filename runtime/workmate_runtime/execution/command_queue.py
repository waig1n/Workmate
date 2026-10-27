"""Bounded Streams intake. Redis locates commands; durable PG facts decide ACK.

Transport ownership never grants execution authority. In-flight entries remain
in the PEL, and reclaim inspects PG before moving any message to this consumer.
"""
import time
from workmate_product.coordination.command_queue import QueueRoute
from .command_transaction import command_transaction, cancel_command_once


class CommandQueueSource:
    def __init__(self, queue, sessions, *, wakeup_bus=None, pending_idle_ms=30000, max_generations=20):
        from workmate_product.persistence.repositories import command_deliveries
        self.queue, self.sessions = queue, sessions
        self.repository = command_deliveries
        self.wakeup_bus = wakeup_bus
        self.pending_idle_ms = pending_idle_ms
        self.max_generations = max_generations
        self._held = {}
        self._rejected = set()
        self._commands = {}
        self._worker_id = self._node_id = None
        self._recovery_at = {}
        self._recovery_cursor = {}
        self._intake_limits = {False: 0, True: 0}

    def control_ids(self, session_id):
        return tuple(command['id'] for command in self._commands.values()
            if command['session_id'] == session_id and command['kind'] in {'StopTask','ResumeInteraction'})

    def control_generation(self, command_id):
        return next((command["delivery_generation"] for command in self._commands.values()
            if command["id"] == command_id),None)

    @cancel_command_once
    async def _classify(self, envelope):
        async with command_transaction(self.sessions) as db:
            if (envelope.command_id,envelope.delivery_generation) in self._rejected:
                await self.repository.quarantine_unclaimed(db,envelope.command_id,envelope.delivery_generation)
            result = await self.repository.classify(db, envelope,
                worker_id=self._worker_id, node_id=self._node_id,max_generations=self.max_generations)
            if result.action == 'wait' and await self.repository.quarantine_abandoned(
                    db,envelope.command_id,envelope.delivery_generation):
                result = await self.repository.classify(db,envelope,
                    worker_id=self._worker_id,node_id=self._node_id,max_generations=self.max_generations)
            return result

    @cancel_command_once
    async def _quarantine(self, route, message, reason):
        async with command_transaction(self.sessions) as db:
            await self.repository.quarantine_message(db,
                stream=self.queue.stream_key(route), message_id=message.message_id,
                command_id=message.envelope.command_id if message.envelope else None,
                error=reason)
        await self.queue.ack(route, [message.message_id])

    async def _resolve(self, route, message):
        key = (route,message.message_id)
        if message.protocol_error or message.envelope is None:
            await self._quarantine(route,message,message.protocol_error or 'invalid_envelope')
            self._held.pop(key,None)
            self._commands.pop(key,None)
            self._prune_rejected()
            return
        try:
            result = await self._classify(message.envelope)
        except ValueError:
            await self._quarantine(route,message,'invalid_delivery')
            self._held.pop(key,None)
            self._commands.pop(key,None)
            self._prune_rejected()
            return
        if key not in self._held:
            return  # Another settlement ACKed this delivery while PG I/O awaited.
        # A cold Resume acquires a session owner after consuming its execution
        # delivery. Its durable generation keeps that channel until rerouted;
        # the new owner does not turn the in-flight message into a control one.
        if result.action != 'ack' and result.command is not None and route.control != (result.command['delivery_channel']=='control'):
            await self._quarantine(route,message,'channel_mismatch')
            self._held.pop(key,None)
            self._commands.pop(key,None)
            self._prune_rejected()
            return
        if result.action == 'ack':
            await self.queue.ack(route,[message.message_id])
            self._held.pop(key,None)
            self._commands.pop(key,None)
            self._prune_rejected()
        elif result.action in {'execute','control'}:
            self._commands[key] = {**result.command, "delivery_generation": message.envelope.delivery_generation}
            if self.wakeup_bus is not None:
                self.wakeup_bus.signal_local('commands')
        else:
            self._commands.pop(key,None)

    async def rejected(self, command_id, observed_generation):
        if type(observed_generation) is not int or observed_generation < 1:
            raise ValueError("Rejection requires the generation observed before execution")
        # Record before I/O, tied to the attempted delivery, never the latest one.
        rejected = [(route,message) for (route,_),message in tuple(self._held.items())
            if message.envelope is not None and message.envelope.command_id == command_id
                and message.envelope.delivery_generation == observed_generation]
        self._rejected.update((message.envelope.command_id,message.envelope.delivery_generation)
            for _,message in rejected)
        for route,message in rejected:
            await self._resolve(route,message)

    async def settle(self):
        for (route,_), message in tuple(self._held.items()):
            await self._resolve(route,message)
        self._prune_rejected()

    def _prune_rejected(self):
        self._rejected.intersection_update((message.envelope.command_id,message.envelope.delivery_generation)
            for message in self._held.values() if message.envelope is not None)

    async def _recover(self, route, count):
        now = time.monotonic()
        if now < self._recovery_at.get(route,0):
            return []
        self._recovery_at[route] = now + max(1,self.pending_idle_ms/2000)
        found=[]
        pending_entries = await self.queue.pending(route,min_idle_ms=self.pending_idle_ms,
            count=count,start=self._recovery_cursor.get(route,'-'))
        self._recovery_cursor[route] = ('('+pending_entries[-1].message_id
            if len(pending_entries)==count else '-')
        for pending in pending_entries:
            if (route,pending.message_id) in self._held:
                continue
            message = await self.queue.peek(route,pending.message_id)
            if message is None:
                continue  # Outbox repair owns lost payloads, not blind replay.
            if message.envelope is not None and not message.protocol_error:
                try:
                    result = await self._classify(message.envelope)
                except ValueError:
                    result = None  # Claim to durably quarantine malformed identity.
                if result is not None and result.action == 'wait':
                    continue  # Never steal a live or uncertain claimed execution.
            claimed = await self.queue.claim_pending(route,self._worker_id,
                [pending.message_id],min_idle_ms=self.pending_idle_ms)
            found.extend(claimed)
        return found

    async def poll(self, *, worker_id, node_id, capacity, control_capacity):
        self._worker_id,self._node_id = worker_id,node_id
        for control,budget in ((True,control_capacity),(False,capacity)):
            self._intake_limits[control] = max(self._intake_limits[control],budget)
            outstanding = sum(route.control == control for route,_ in self._held)
            remaining = min(budget,max(0,self._intake_limits[control]-outstanding))
            for target in (worker_id,None):
                if not remaining:
                    break
                route=QueueRoute(node_id,target,control)
                await self.queue.ensure_group(route)
                messages=await self._recover(route,remaining)
                if len(messages)<remaining:
                    messages.extend(await self.queue.read(route,worker_id,count=remaining-len(messages)))
                for message in messages:
                    self._held[(route,message.message_id)] = message
                    await self._resolve(route,message)
                remaining -= len(messages)
        result=[]
        for command in self._commands.values():
            if (command['session_owner_id'] == worker_id and command['kind'] != 'StartTask'
                    and command['session_status'] in {'running','stopping','awaiting_interaction'}):
                continue  # Existing running/parked owner is the sole control driver.
            candidate=dict(command)
            if command['kind']=='StopTask':
                candidate['stop_phase'] = ('unclaimed' if command['session_active_task_id'] is None
                    else 'terminal' if command['session_status'] in {'completed','failed','interrupted'}
                    else 'suspended')
            result.append(candidate)
        return result
