"""Authorized channel management and owner-bound identity linking."""
import hashlib
import secrets
import uuid
from types import SimpleNamespace
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from workmate_product.application.authorization import AccessDecisionService
from workmate_product.application.agent_binding_status import binding_issues
from workmate_product.persistence.models import (Agent, Scope, User, Task, SecurityAuditEvent,
    ChannelConnection, ChannelBindingCode, ChannelIdentity, ChannelOutbox)
from workmate_product.persistence.repositories import channels as repo
from workmate_product.persistence.repositories.task_capacity import lock_capacity
from .rules import ChannelCreate, credentials_for, KEYS
from ..services.public_urls import WorkmateLinks
from .adapters.wechat import WechatAdapter, validate_base_url


def audit(db, actor, channel_id, action):
    db.add(SecurityAuditEvent(id=str(uuid.uuid4()), event_type='channel_' + action,
        actor_user_id=actor, metadata_={'channel_id': channel_id}))


class ChannelManagement:
    def __init__(self, sessions, admission, policy, public_url):
        self.sessions, self.admission, self.policy = sessions, admission, policy
        self.links = WorkmateLinks(public_url)

    async def permission(self, db, user_id):
        root = await repo.root(db)
        return bool(root and await AccessDecisionService(db).has_permission(user_id, root.id, 'channel.manage'))

    def enabled(self, *, wechat=False):
        if not self.policy.enabled or wechat and not self.policy.wechat_enabled:
            raise HTTPException(503, '消息渠道功能未启用')

    async def get(self, db, user_id, channel_id, *, manage=False, lock=False):
        row = await repo.channel_by_id(db, channel_id, lock=lock)
        if not row or row.deleted_at or row.owner_user_id not in (None, user_id):
            raise HTTPException(404, '渠道不存在')
        if manage and row.owner_user_id != user_id and not await self.permission(db, user_id):
            raise HTTPException(403, '没有渠道管理权限')
        return row

    async def view(self, db, user_id, row):
        can_manage = row.owner_user_id == user_id or await self.permission(db, user_id)
        agent = await db.get(Agent, row.agent_id)
        status = row.status
        if status == 'connected' and (not row.lease_until or row.lease_until <= await repo.now(db)):
            status = 'disconnected'
        return {key: getattr(row, key) for key in
            ('id','name','provider','agent_id','enabled','version','status','owner_user_id')} | {
            'status': status,
            'agent_name': agent.display_name if agent else '不可用 Agent',
            'last_error': row.last_error if can_manage else None,
            'group_enabled': row.group_enabled,
            'group_allowlist': row.group_allowlist if can_manage else [],
            'has_credentials': bool(row.credentials and '_qr' not in row.credentials),
            'can_manage': can_manage}

    async def list(self, db, user_id):
        return {'items': [await self.view(db, user_id, row) for row in await repo.connections(db, user_id)],
            'can_manage': await self.permission(db, user_id), 'wechat_enabled': self.policy.wechat_enabled,
            'enabled': self.policy.enabled}

    async def validate_agent(self, db, user_id, agent_id, *, personal):
        access = AccessDecisionService(db)
        agent = await db.get(Agent, agent_id)
        if not agent or agent.deleted_at or not await access.can_view_agent(user_id, agent_id):
            raise HTTPException(422, '默认 Agent 不可用')
        scope = await db.get(Scope, agent.scope_id)
        if personal:
            if not await self.admission._authorize(db, user_id, agent_id):
                raise HTTPException(403, '当前 Agent 或能力不可用')
        else:
            await self.validate_target(db,agent_id)

    async def validate_target(self, db, agent_id):
        """Organization binding health, independent of the manager's personal use grants."""
        agent = await db.get(Agent,agent_id)
        scope = await db.get(Scope,agent.scope_id) if agent else None
        target = await repo.root(db)
        if (not agent or agent.deleted_at or not scope or scope.kind == 'personal' or not target
            or agent.visibility != 'public'
            or not await repo.agent_shared_to_scope(db, agent.id, target.id)):
            raise HTTPException(422, '企业渠道请选择已发布并共享到组织的 Agent')
        access = AccessDecisionService(db)
        if await binding_issues(db, SimpleNamespace(id=agent.id, scope_id=target.id,
            knowledge_config=agent.knowledge_config), access,
            knowledge_enabled=getattr(self.admission, 'knowledge_enabled', True)):
            raise HTTPException(422, '默认 Agent 的能力绑定不可用')

    async def create(self, db, user_id, body):
        self.enabled(wechat=body.provider == 'wechat')
        await lock_capacity(db)
        personal = body.provider == 'wechat'
        if not personal and not await self.permission(db, user_id):
            raise HTTPException(403, '没有渠道管理权限')
        rows = await repo.connections(db)
        if len(rows) >= self.policy.max_connections:
            raise HTTPException(429, '渠道数量已达上限')
        await self.validate_agent(db, user_id, body.agent_id, personal=personal)
        try:
            credentials = credentials_for(body.provider, body.credentials)
        except ValueError:
            raise HTTPException(422, '渠道凭据字段无效') from None
        root = await repo.root(db)
        if root is None:
            raise HTTPException(409, '组织尚未就绪')
        key = None if personal else body.provider + ':' + hashlib.sha256(
            credentials[KEYS[body.provider][0]].encode()).hexdigest()
        row = ChannelConnection(id=str(uuid.uuid4()), name=body.name, provider=body.provider,
            agent_id=body.agent_id, scope_id=root.id, owner_user_id=user_id if personal else None,
            credentials=credentials, bot_key=key, enabled=False,
            group_enabled=body.group_enabled, group_allowlist=body.group_allowlist)
        db.add(row)
        audit(db, user_id, row.id, 'created')
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, '该机器人已配置，请使用现有渠道') from None
        await db.refresh(row)
        return await self.view(db, user_id, row)

    async def update(self, db, user_id, channel_id, body):
        await lock_capacity(db)
        row = await self.get(db, user_id, channel_id, manage=True, lock=True)
        if row.version != body.version:
            raise HTTPException(409, '渠道配置已更新，请刷新')
        if body.enabled and not row.enabled or body.agent_id != row.agent_id:
            self.enabled(wechat=row.provider == 'wechat')
            await self.validate_agent(db, user_id, body.agent_id, personal=row.provider == 'wechat')
        try:
            ChannelCreate(name=body.name,provider=row.provider,agent_id=body.agent_id,
                group_enabled=body.group_enabled,group_allowlist=body.group_allowlist)
            if body.credentials is not None:
                values = credentials_for(row.provider, body.credentials)
                if row.provider == 'wechat':
                    raise ValueError('Use QR binding')
                id_field = KEYS[row.provider][0]
                if values[id_field] != row.credentials.get(id_field):
                    raise ValueError('Bot identity cannot be replaced')
                row.credentials = values
        except ValueError:
            raise HTTPException(422, '凭据或群配置无效，更换机器人需新建渠道') from None
        if body.enabled and (not row.credentials or '_qr' in row.credentials):
            raise HTTPException(422, '请先扫码登录微信' if row.provider == 'wechat' else '请先完成机器人凭据配置')
        if body.enabled and not self.links.configured:
            raise HTTPException(422, '未配置工作台访问地址，请管理员设置 WORKMATE_PUBLIC_URL 为员工可访问的前端地址')
        for key in ('name','agent_id','enabled','group_enabled','group_allowlist'):
            setattr(row, key, getattr(body,key))
        row.version += 1
        row.last_error = None
        audit(db, user_id, row.id, 'updated')
        await db.commit()
        return await self.view(db, user_id, row)

    async def delete(self, db, user_id, channel_id, version):
        await lock_capacity(db)
        row = await self.get(db, user_id, channel_id, manage=True, lock=True)
        if row.version != version:
            raise HTTPException(409, '渠道配置已更新，请刷新')
        row.enabled, row.deleted_at = False, await repo.now(db)
        row.version += 1
        row.credentials = {}
        row.bot_key = None
        audit(db, user_id, row.id, 'deleted')
        await db.commit()

    async def binding_code(self, db, user_id, channel_id):
        self.enabled()
        await lock_capacity(db)
        row = await self.get(db, user_id, channel_id, lock=True)
        if not row.enabled or row.provider == 'wechat':
            raise HTTPException(409, '请选择已启用的企业渠道')
        instant = await repo.now(db)
        if await repo.codes_count(db, user_id, instant) >= 5:
            raise HTTPException(429, '绑定操作过于频繁，请稍后重试')
        await repo.expire_codes(db, user_id, channel_id, instant)
        code = secrets.token_urlsafe(18)
        expiry = instant + timedelta(minutes=5)
        db.add(ChannelBindingCode(code_hash=hashlib.sha256(code.encode()).hexdigest(),
            channel_id=channel_id, user_id=user_id, expires_at=expiry))
        audit(db, user_id, row.id, 'binding_requested')
        await db.commit()
        return {'code': code, 'expires_at': expiry}

    async def bindings(self, db, user_id):
        items = []
        for value in await repo.identities(db, user_id):
            channel = await repo.channel_by_id(db, value.channel_id)
            if channel and not channel.deleted_at:
                items.append({'id':value.id,'channel_id':channel.id,'channel_name':channel.name,
                    'provider':channel.provider,'external_user_id':value.external_user_id,
                    'created_at':value.created_at})
        return {'items':items}

    async def unbind(self, db, user_id, identity_id):
        value = await db.get(ChannelIdentity, identity_id)
        if value is None or value.user_id != user_id:
            raise HTTPException(404, '绑定不存在')
        await repo.channel_by_id(db, value.channel_id, lock=True)
        audit(db, user_id, value.channel_id, 'unbound')
        await db.delete(value)
        await db.commit()

    async def deliveries(self, db, user_id, channel_id, limit=20, offset=0):
        await self.get(db,user_id,channel_id)
        rows = await repo.delivery_page(db,channel_id,user_id,limit,offset)
        items = []
        for row in rows:
            task = await db.get(Task,row.task_id) if row.task_id else None
            items.append({'id':row.id,'task_id':row.task_id,'status':row.status,
                'last_error':row.last_error,'conversation_id':task.conversation_id if task else None,
                'created_at':row.created_at})
        return {'items':items}

    async def retry(self, db, user_id, delivery_id):
        row = await db.get(ChannelOutbox, delivery_id, with_for_update=True)
        if not row or row.user_id != user_id:
            raise HTTPException(404, '交付记录不存在')
        if row.schedule_occurrence_id:
            raise HTTPException(409, '请使用定时任务通知重试入口')
        retries = row.reply_state.get('manual_retries',0) if row.kind == 'reply' else row.attempts - 1
        if row.status not in {'unknown','failed'} or retries >= 4:
            raise HTTPException(409, '该交付不可重试')
        if row.kind == 'reply':
            row.reply_state = {**row.reply_state, 'manual_retries': retries + 1}
        row.status, row.next_attempt_at = 'pending', await repo.now(db)
        audit(db,user_id,row.channel_id,'delivery_retry')
        await db.commit()
        return {'status':'pending'}

    async def qr_owner(self, db, user_id, channel_id, *, lock=False):
        self.enabled(wechat=True)
        actor = await db.get(User,user_id,populate_existing=True)
        if not actor or actor.status != 'active':
            raise HTTPException(403, '账号不可用')
        row = await self.get(db, user_id, channel_id, lock=lock)
        if row.provider != 'wechat' or row.owner_user_id != user_id:
            raise HTTPException(403, '仅本人可以登录个人微信')
        return row

    @staticmethod
    def qr_matches(row, user_id, qrcode, instant):
        pending = (row.credentials or {}).get('_qr', {})
        try:
            return (pending.get('user_id') == user_id and pending.get('qrcode') == qrcode
                and datetime.fromisoformat(pending['expires_at']) > instant)
        except (KeyError, ValueError, TypeError):
            return False

    async def qr(self, db, user_id, channel_id):
        row = await self.qr_owner(db, user_id, channel_id)
        version = row.version
        await db.rollback()  # Provider network calls never hold a transaction or row lock.
        adapter = WechatAdapter({})
        try:
            result = await adapter.get_qrcode()
            qrcode, url = result['qrcode'], result['qrcode_img_content']
            parsed = urlsplit(url)
            if (not isinstance(qrcode, str) or not qrcode or len(qrcode) > 4096
                or parsed.scheme != 'https' or parsed.hostname != 'liteapp.weixin.qq.com'
                or parsed.port not in (None,443) or parsed.username or parsed.password):
                raise ValueError('Invalid login response')
        except Exception:
            raise HTTPException(502, '微信登录暂时不可用，请重试') from None
        finally:
            await adapter.close()
        await lock_capacity(db)
        row = await self.qr_owner(db,user_id,channel_id,lock=True)
        if row.version != version:
            raise HTTPException(409, '渠道配置已更新，请重新获取二维码')
        instant = await repo.now(db)
        row.enabled = False
        row.credentials = {'_qr': {'qrcode':qrcode,'user_id':user_id,
            'expires_at':(instant+timedelta(minutes=5)).isoformat()}}
        row.version += 1
        audit(db,user_id,channel_id,'qr_requested')
        await db.commit()
        return {'qrcode':qrcode,'qrcode_url':url}

    async def qr_status(self, db, user_id, channel_id, qrcode):
        row = await self.qr_owner(db,user_id,channel_id)
        if not self.qr_matches(row,user_id,qrcode,await repo.now(db)):
            raise HTTPException(409, '二维码已过期或已更换，请重新获取')
        version = row.version
        await db.rollback()
        adapter = WechatAdapter({})
        try:
            result = await adapter.get_qrcode_status(qrcode)
            status = result.get('status')
            if status not in {'wait','waiting','scaned','scanned','confirmed','expired'}:
                raise ValueError('Unknown login state')
            if status == 'confirmed':
                keys = ('bot_token','ilink_bot_id','ilink_user_id')
                if any(not isinstance(result.get(k),str) or not result[k] or len(result[k]) > (4096 if k=='bot_token' else 256) for k in keys):
                    raise ValueError('Missing identity')
                credentials = {k:result[k] for k in keys}
                credentials['base_url'] = validate_base_url(result.get('baseurl') or 'https://ilinkai.weixin.qq.com')
        except Exception:
            raise HTTPException(502, '微信登录状态暂时不可用，请重试') from None
        finally:
            await adapter.close()
        await lock_capacity(db)
        row = await self.qr_owner(db,user_id,channel_id,lock=True)
        if row.version != version or not self.qr_matches(row,user_id,qrcode,await repo.now(db)):
            raise HTTPException(409, '二维码已过期或已更换，请重新获取')
        if status == 'confirmed':
            self.enabled(wechat=True)
            await self.validate_agent(db, user_id, row.agent_id, personal=True)
            if not self.links.configured:
                raise HTTPException(422, '未配置工作台访问地址，请管理员设置 WORKMATE_PUBLIC_URL 为员工可访问的前端地址')
            identity = await repo.identity(db,channel_id,credentials['ilink_user_id'])
            row.credentials = credentials
            key = 'wechat:' + hashlib.sha256(credentials['ilink_bot_id'].encode()).hexdigest()
            # iLink can rotate bot IDs. Only the same channel's already-bound
            # external scanner identity and Workmate owner may renew a login.
            same_bound_account = (identity is not None
                and identity.channel_id == channel_id
                and identity.external_user_id == credentials['ilink_user_id']
                and identity.user_id == user_id == row.owner_user_id)
            if row.bot_key and not same_bound_account:
                raise HTTPException(409, '更换微信账号需新建渠道')
            row.bot_key = key
            row.enabled = True
            row.cursor = ''
            row.version += 1
            row.last_error = None
            if identity and identity.user_id != user_id:
                raise HTTPException(409, '微信身份已绑定')
            if not identity:
                db.add(ChannelIdentity(id=str(uuid.uuid4()),channel_id=channel_id,
                    external_user_id=credentials['ilink_user_id'],user_id=user_id))
            audit(db,user_id,channel_id,'qr_confirmed')
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise HTTPException(409, '该微信账号已有连接') from None
        else:
            await db.rollback()
        return {'status':status}
