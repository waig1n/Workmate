"""Experimental iLink private messages. Cursor commits follow durable ingestion."""
import asyncio
import base64
import secrets
import time
import uuid

from .base import safe_transport, Adapter, ChannelTransportError
from .parsing import normalize

BASE_URL = 'https://ilinkai.weixin.qq.com'
APP_HEADERS = {'iLink-App-Id': 'bot', 'iLink-App-ClientVersion': str(0x020000)}


def validate_base_url(value: str) -> str:
    if value.rstrip('/') != BASE_URL:
        raise ValueError('Unsupported iLink base URL')
    return BASE_URL


class WechatAdapter(Adapter):
    def __init__(self, credentials, *, cursor='', persist_cursor=None):
        self.base_url = validate_base_url(credentials.get('base_url') or BASE_URL)
        super().__init__(credentials)
        self._typing_jobs = {}
        self._typing_desired = {}
        self._typing_backoff = {}
        self._typing_closed = False
        self.cursor = cursor
        self.persist_cursor = persist_cursor

    def headers(self, *, authenticated=True):
        result = {**APP_HEADERS, 'AuthorizationType': 'ilink_bot_token',
            'X-WECHAT-UIN': base64.b64encode(str(secrets.randbelow(2**32)).encode()).decode()}
        if authenticated and self._credentials.get('bot_token'):
            result['Authorization'] = 'Bearer ' + self._credentials['bot_token']
        return result

    async def api(self, path, body):
        result = await self.request('POST', self.base_url + '/ilink/bot/' + path,
            headers=self.headers(), json={**body, 'base_info': {'channel_version': '2.0.0', 'bot_agent': 'Workmate/2.0'}},
            timeout=45 if path == 'getupdates' else 20)
        if result.get('ret') == -14 or result.get('errcode') == -14:
            raise ChannelTransportError('wechat_session_expired')
        self.check(result, 'ret')
        self.check(result)
        return result

    async def get_qrcode(self):
        result = await self.request('POST', BASE_URL + '/ilink/bot/get_bot_qrcode',
            params={'bot_type': 3}, json={'local_token_list': []}, headers=self.headers(authenticated=False))
        self.check(result, 'ret')
        return {key: result[key] for key in ('qrcode', 'qrcode_img_content')}

    async def get_qrcode_status(self, qrcode):
        result = await self.request('GET', BASE_URL + '/ilink/bot/get_qrcode_status',
            params={'qrcode': qrcode}, headers=dict(APP_HEADERS), timeout=45)
        self.check(result, 'ret')
        if result.get('status') == 'confirmed':
            validate_base_url(result.get('baseurl') or BASE_URL)
        return result

    async def poll_once(self, emit):
        if self.persist_cursor is None:
            raise ChannelTransportError('cursor_persistence_required')
        data = await self.api('getupdates', {'get_updates_buf': self.cursor})
        for item in data.get('msgs') or []:
            if item.get('message_type') == 1:
                await emit(normalize('wechat', item))
        cursor = data.get('get_updates_buf') or self.cursor
        await self.persist_cursor(cursor)
        self.cursor = cursor

    async def run(self, emit):
        self.ready.set()
        while True:
            await self.poll_once(emit)
            await asyncio.sleep(0.1)

    @safe_transport
    async def send(self, target, text):
        context = (target.get('reply_context') or {}).get('context_token')
        if not context:
            raise ChannelTransportError('wechat_reply_context_missing')
        client_id = str(uuid.uuid4())
        await self.api('sendmessage', {'msg': {'from_user_id': '', 'to_user_id': target['sender_id'],
            'client_id': client_id, 'message_type': 2, 'message_state': 2, 'context_token': context,
            'item_list': [{'type': 1, 'text_item': {'text': text}}]}})
        return client_id

    def supports_typing(self):
        return True

    async def sync_typing(self, targets):
        """Refresh a five-second authorization lease; never wait for network."""
        if self._typing_closed:
            return
        now = time.monotonic()
        desired = {}
        for target in targets:
            sender = target.get('sender_id')
            context = (target.get('reply_context') or {}).get('context_token')
            if target.get('chat_type') == 'private' and sender and context:
                desired[sender] = (context, now + 5)
                if len(desired) >= 64:
                    break
        self._typing_desired = desired
        self._typing_backoff = {key: until for key, until in self._typing_backoff.items() if until > now}
        for sender, (context, job) in list(self._typing_jobs.items()):
            if job.done():
                del self._typing_jobs[sender]
            elif sender not in desired or desired[sender][0] != context:
                if not job.cancelling():
                    job.cancel()
        for sender, (context, _) in desired.items():
            if sender not in self._typing_jobs and len(self._typing_jobs) < 64 and self._typing_backoff.get(sender, 0) <= now:
                self._typing_jobs[sender] = (context, asyncio.create_task(self._typing_loop(sender, context)))

    def _typing_authorized(self, sender, context):
        current = self._typing_desired.get(sender)
        return bool(current and current[0] == context and current[1] > time.monotonic())

    async def _typing_loop(self, sender, context):
        ticket = None
        try:
            async with asyncio.timeout(2):
                config = await self.api('getconfig', {'ilink_user_id': sender, 'context_token': context})
                candidate = config.get('typing_ticket')
                if not isinstance(candidate, str) or not candidate:
                    raise ChannelTransportError('wechat_typing_unavailable')
                ticket = candidate
            while self._typing_authorized(sender, context):
                remaining = self._typing_desired[sender][1] - time.monotonic()
                async with asyncio.timeout(min(2, remaining)):
                    await self.api('sendtyping', {'ilink_user_id': sender, 'typing_ticket': ticket, 'status': 1})
                current = self._typing_desired.get(sender)
                if not current or current[0] != context:
                    break
                await asyncio.sleep(max(0, min(3, current[1] - time.monotonic())))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Typing is cosmetic. Do not log payload, ticket or provider error.
            self._typing_backoff[sender] = time.monotonic() + 3
        finally:
            if ticket:
                try:
                    async with asyncio.timeout(0.8):
                        await self.api('sendtyping', {'ilink_user_id': sender, 'typing_ticket': ticket, 'status': 2})
                except (Exception, asyncio.CancelledError):
                    pass

    async def stop_typing(self, target):
        sender = target.get('sender_id')
        context = (target.get('reply_context') or {}).get('context_token')
        desired = self._typing_desired.get(sender)
        if desired and desired[0] != context:
            return
        self._typing_desired.pop(sender, None)
        entry = self._typing_jobs.get(sender)
        if entry:
            job = entry[1]
            if not job.cancelling():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            if self._typing_jobs.get(sender) is entry:
                self._typing_jobs.pop(sender, None)

    @safe_transport
    async def close(self):
        self._typing_closed = True
        self._typing_desired.clear()
        jobs = [entry[1] for entry in self._typing_jobs.values()]
        for job in jobs:
            if not job.cancelling():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self._typing_jobs.clear()
        self._typing_backoff.clear()
        await super().close()
