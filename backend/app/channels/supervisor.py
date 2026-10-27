"""Gateway-owned bounded connections, fenced ingestion and independent delivery."""
import asyncio
import logging
import time
import uuid
from datetime import timedelta, datetime
from dataclasses import dataclass
from workmate_product.persistence.models import ChannelInbox, Conversation, Task, User
from workmate_product.persistence.repositories import channels as repo
from .adapters import create_adapter
from .adapters.base import ChannelTransportError
from .reply_transport import plan_reply, deliver_reply

logger = logging.getLogger(__name__)


@dataclass
class ConnectionRunner:
    epoch: int
    version: int
    task: asyncio.Task
    stop: asyncio.Event


class ChannelSupervisor:
    def __init__(self, service, *, adapter_factory=create_adapter, text_feed=None):
        self.service = service
        self.sessions = service.sessions
        self.policy = service.policy
        self.factory = adapter_factory
        self.text_feed = text_feed
        self.live = {}
        self.owner = str(uuid.uuid4())
        self.runners = {}
        self.retries = {}

    async def allowed_delivery(self, db, channel, row):
        if row.schedule_occurrence_id:
            from .scheduled_notifications import allowed_delivery
            return await allowed_delivery(db,row,self.service.admission)
        if row.target.get('chat_type') == 'group' and (not channel.group_enabled
                or row.target.get('chat_id') not in channel.group_allowlist):
            return False
        if row.user_id is None:
            return row.kind in ('binding','rejected','unsupported')
        identity = await repo.identity(db,channel.id,row.target.get('sender_id'))
        if not identity or identity.user_id != row.user_id:
            return False
        user = await db.get(User,row.user_id)
        if not user or user.status != 'active':
            return False
        if not row.task_id:
            return True
        task = await db.get(Task,row.task_id)
        conv = await db.get(Conversation,task.conversation_id) if task else None
        if not conv or conv.deleted_at or conv.user_id != row.user_id:
            return False
        # Owner safety-control receipts contain fixed text, never task content.
        return row.kind == 'control' or bool(await self.service.admission._authorize(db,row.user_id,conv.agent_id))

    async def send_one(self, channel_id, epoch, version, adapter):
        async with self.sessions() as db:
            channel = await repo.lease_valid(db,channel_id,self.owner,epoch,version)
            if not channel:
                return False
            delivery_owner = str(uuid.uuid4())
            row = await repo.next_outbox(db,channel_id,delivery_owner,self.policy.lease_secs)
            if not row:
                await db.commit()  # Persist expired sending -> unknown even when no next item.
                return False
            channel.lease_until = await repo.now(db) + timedelta(seconds=self.policy.lease_secs)
            if not await self.allowed_delivery(db,channel,row):
                row.status = 'cancelled'
                row.last_error = '身份、群范围或访问权限已失效'
                await db.commit()
                return True
            id,target,text = row.id,dict(row.target),row.text
            revision, finished, kind = row.reply_revision, row.reply_finished, row.kind
            state, receipt, bootstrap = None, None, False
            if row.kind == 'reply':
                instant = await repo.now(db)
                task = await db.get(Task,row.task_id)
                if not row.reply_finished and task and task.status in (
                        'completed','failed','cancelled','interrupted','awaiting_permission','awaiting_external_result'):
                    row.status = 'pending'
                    row.next_attempt_at = instant + timedelta(seconds=self.policy.poll_interval_secs)
                    await db.commit()
                    return True
                target,text,receipt,state,bootstrap,defer = plan_reply(row,adapter,instant)
                row.reply_state = state
                if defer is not None:
                    row.status, row.next_attempt_at = 'pending', defer
                    await db.commit()
                    return True
                if bootstrap:
                    row.next_attempt_at = datetime.fromisoformat(state['hint_at'])
            sequence = row.attempts
            await db.commit()
        if kind == 'reply' and finished and getattr(adapter, 'supports_typing', lambda: False)():
            try:
                async with asyncio.timeout(1):
                    await adapter.stop_typing(target)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Optional feedback must never prevent the final reply.
        status,error = 'unknown','发送回执未知，请核实后重试'
        try:
            # Network occurs after the short claim transaction. This attempt is
            # bounded below the lease; uncertain attempts are never auto-replayed.
            async with asyncio.timeout(min(25,self.policy.lease_secs-5)):
                if state is not None:
                    receipt,state = await deliver_reply(adapter,target,text,receipt,state,
                        finished=finished,sequence=sequence)
                else:
                    receipt = await adapter.send(target,text)
            receipt = str(receipt)[:256] if isinstance(receipt,(str,int)) else None
            status,error = 'sent',None
        except ChannelTransportError as exc:
            if str(exc) in ('platform_rejected_request','invalid_platform_endpoint'):
                status,error = 'failed','平台拒绝发送，请检查配置后重试'
                if bootstrap and str(exc) == 'platform_rejected_request':
                    status,error = 'sent',None
                    state = {**state, 'mode':'plain'}
        except asyncio.CancelledError:
            # Lease expiry recovery will mark the unfinished sending row unknown.
            raise
        except Exception:
            pass  # Never persist SDK exception URLs, tokens or message bodies.
        async with self.sessions() as db:
            await repo.finish_delivery(db,id,delivery_owner,status,receipt=receipt,error=error,
                revision=0 if bootstrap else revision, state=state,
                interval=self.policy.stream_interval_secs)
            await db.commit()
        return True

    async def cursor(self, channel_id, epoch, version, cursor):
        if not isinstance(cursor,str) or len(cursor) > 65536:
            raise ValueError('invalid_cursor')
        async with self.sessions() as db:
            row = await repo.lease_valid(db,channel_id,self.owner,epoch,version)
            if not row:
                raise ValueError('channel_lease_lost')
            row.cursor = cursor
            await db.commit()

    async def _sync_typing(self, channel_id, epoch, version, adapter):
        if not getattr(adapter, 'supports_typing', lambda: False)():
            return
        desired = []
        try:
            async with self.sessions() as db:
                channel = await repo.lease_valid(db,channel_id,self.owner,epoch,version)
                if channel:
                    for inbox in await repo.live_replies(db,channel_id,
                            min(64,self.policy.max_live_replies),include_plain=True):
                        row = await repo.reply_slot(db,inbox.id)
                        if row and await self.allowed_delivery(db,channel,row):
                            desired.append(dict(inbox.target))
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            desired = []
        try:
            # Adapter only synchronizes bounded in-memory tasks here; their
            # authorization expires if this lease check stops being refreshed.
            await adapter.sync_typing(desired)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug('Channel typing synchronization deferred')

    async def _sync_live(self, channel_id, epoch, version, adapter):
        if self.text_feed is None:
            return
        desired = {}
        async with self.sessions() as db:
            channel = await repo.lease_valid(db,channel_id,self.owner,epoch,version)
            if channel:
                for inbox in await repo.live_replies(db,channel_id,self.policy.max_live_replies):
                    row = await repo.reply_slot(db,inbox.id)
                    if (adapter.supports_stream(inbox.target)
                            and row.reply_state.get('mode') != 'plain'
                            and await self.allowed_delivery(db,channel,row)):
                        desired[inbox.id] = (inbox.conversation_id,inbox.task_id,inbox.user_id)
            await db.commit()
        for key,(connection_id,task) in list(self.live.items()):
            if connection_id == channel_id and (key not in desired or task.done()):
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                self.live.pop(key,None)
        for key,identity in desired.items():
            if key not in self.live and len(self.live) < self.policy.max_live_replies:
                self.live[key] = (channel_id,asyncio.create_task(self._watch_reply(key,*identity,epoch,version)))

    async def _watch_reply(self, inbox_id, conversation_id, task_id, user_id, epoch, version):
        latest = None
        written = None
        changed = asyncio.Event()

        async def receive(text):
            nonlocal latest
            latest = text
            changed.set()

        watcher = asyncio.create_task(self.text_feed.watch(conversation_id,task_id,user_id,receive))
        try:
            while not watcher.done():
                try:
                    await asyncio.wait_for(changed.wait(),self.policy.stream_interval_secs)
                except TimeoutError:
                    continue
                changed.clear()
                if latest != written:
                    await self.service.update_reply(inbox_id,latest,owner=self.owner,epoch=epoch,version=version)
                    written = latest
                await asyncio.sleep(self.policy.stream_interval_secs)
            await watcher
        except asyncio.CancelledError:
            raise
        except Exception:
            # A fresh authorized reset is attempted by the bounded connection
            # loop. The durable final result does not depend on this live path.
            logger.debug('Channel live reply reset required')
        finally:
            watcher.cancel()
            await asyncio.gather(watcher,return_exceptions=True)

    async def _send_loop(self, channel_id, epoch, version, adapter, stop):
        await adapter.ready.wait()
        async with self.sessions() as db:
            row = await repo.lease_valid(db,channel_id,self.owner,epoch,version)
            if not row:
                return
            row.status,row.last_error = 'connected',None
            await db.commit()
        while not stop.is_set():
            await self._sync_typing(channel_id,epoch,version,adapter)
            await self._sync_live(channel_id,epoch,version,adapter)
            for _ in range(self.policy.batch_size):
                if stop.is_set() or not await self.send_one(channel_id,epoch,version,adapter):
                    break
            try:
                await asyncio.wait_for(stop.wait(),min(self.policy.poll_interval_secs,self.policy.stream_interval_secs))
            except TimeoutError:
                pass

    async def _connection(self, channel, epoch, version, stop):
        adapter = None
        tasks = []
        try:
            async def persist(value):
                await self.cursor(channel.id,epoch,version,value)
            adapter = self.factory(channel.provider,dict(channel.credentials),cursor=channel.cursor,
                persist_cursor=persist)
            async def emit(message):
                await self.service.ingest(channel.id,message,owner=self.owner,epoch=epoch,version=version)
            tasks = [asyncio.create_task(adapter.listen(emit,stop)),
                     asyncio.create_task(self._send_loop(channel.id,epoch,version,adapter,stop))]
            done,_ = await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.sessions() as db:
                row = await repo.lease_valid(db,channel.id,self.owner,epoch,version)
                if row:
                    row.status,row.last_error = 'error','连接中断，请检查平台配置、权限或网络'
                    if isinstance(exc,ChannelTransportError) and str(exc) == 'wechat_session_expired':
                        row.enabled = False
                        row.version += 1
                        row.last_error = '微信登录已过期，连接已停用，请重新扫码后启用'
                    await db.commit()
        finally:
            stop.set()
            for key,(connection_id,task) in list(self.live.items()):
                if connection_id == channel.id:
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
                    self.live.pop(key,None)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            if adapter:
                try:
                    await asyncio.wait_for(adapter.close(),5)
                except Exception:
                    pass

    async def connections(self):
        async with self.sessions() as db:
            values = await repo.connections(db,enabled=True)
            ids = {row.id for row in values if row.provider != 'wechat' or self.policy.wechat_enabled}
        for id,runner in list(self.runners.items()):
            if id not in ids or runner.task.done():
                runner.stop.set()
                runner.task.cancel()
                await asyncio.gather(runner.task,return_exceptions=True)
                self.runners.pop(id)
                attempt,_ = self.retries.get(id,(0,0))
                self.retries[id] = (attempt+1,time.monotonic()+min(60,2**min(attempt+1,6)))
                async with self.sessions() as db:
                    await repo.release(db,id,self.owner)
                    await db.commit()
        for id in ids:
            runner = self.runners.get(id)
            if runner is None and self.retries.get(id,(0,0))[1] > time.monotonic():
                continue
            async with self.sessions() as db:
                row = await repo.claim_connection(db,id,self.owner,self.policy.lease_secs)
                if row is None:
                    if runner:
                        runner.stop.set()
                        runner.task.cancel()
                    continue
                if runner and (runner.epoch != row.lease_epoch or runner.version != row.version):
                    runner.stop.set()
                    runner.task.cancel()
                    await db.commit()
                    continue
                if not runner:
                    row.status = 'connecting'
                await db.commit()
                if not runner:
                    stop = asyncio.Event()
                    task = asyncio.create_task(self._connection(row,row.lease_epoch,row.version,stop))
                    self.runners[id] = ConnectionRunner(row.lease_epoch,row.version,task,stop)
        for id in set(self.retries)-ids:
            self.retries.pop(id,None)

    async def _pause(self, shutdown, *, heartbeat=False):
        try:
            interval = min(self.policy.poll_interval_secs,self.policy.lease_secs / 3) if heartbeat else self.policy.poll_interval_secs
            await asyncio.wait_for(shutdown.wait(),interval)
        except TimeoutError:
            pass

    async def _maintain(self, shutdown):
        # Lease renewal is independent of potentially slow admission/projection.
        while not shutdown.is_set():
            try:
                await self.connections()
            except Exception:
                logger.warning('Channel connection cycle deferred')
            await self._pause(shutdown,heartbeat=True)

    async def _process(self, shutdown):
        while not shutdown.is_set():
            try:
                async with self.sessions() as db:
                    ids = await repo.pending_ids(db,self.policy.batch_size)
                for id in ids:
                    if shutdown.is_set():
                        break
                    try:
                        await self.service.process_one(id)
                    except Exception:
                        logger.warning('Channel inbox processing deferred')
                await self.service.project_results()
                from .scheduled_notifications import project_notifications
                await project_notifications(self.service)
            except Exception:
                logger.warning('Channel background cycle deferred')
            await self._pause(shutdown)

    async def serve(self, shutdown):
        if not self.policy.enabled or not self.service.links.configured:
            await shutdown.wait()
            return
        loops = [asyncio.create_task(self._maintain(shutdown)),
                 asyncio.create_task(self._process(shutdown))]
        try:
            await asyncio.gather(*loops)
        finally:
            for loop in loops:
                loop.cancel()
            await asyncio.gather(*loops,return_exceptions=True)
            for runner in self.runners.values():
                runner.stop.set()
                runner.task.cancel()
            await asyncio.gather(*(r.task for r in self.runners.values()),return_exceptions=True)
            for id in self.runners:
                async with self.sessions() as db:
                    await repo.release(db,id,self.owner)
                    await db.commit()
            self.runners.clear()
