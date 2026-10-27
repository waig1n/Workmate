"""Normalize only authenticated platform events; never infer user identities."""
import json
import re

from app.channels.contracts import InboundMessage


def normalize(provider: str, data: dict, *, bot_id: str = '') -> InboundMessage:
    if provider == 'wecom':
        body = data['body']
        sender = (body.get('from') or {}).get('userid', '')
        group = body.get('chattype') == 'group'
        text = (body.get('text') or {}).get('content', '').strip()
        if group:
            text = re.sub(r'^@\S+\s+(?=/)', '', text).strip()
        return InboundMessage(str(body.get('msgid') or ''), sender,
            body.get('chatid') or sender, 'group' if group else 'private',
            text,
            # WeCom API robots receive group messages only when addressed.
            mentioned=group, reply_context={'req_id': (data.get('headers') or {}).get('req_id', '')}, unsupported=body.get('msgtype') != 'text')
    if provider == 'dingtalk':
        group = str(data.get('conversationType')) == '2'
        return InboundMessage(data.get('msgId', ''), data.get('senderStaffId', ''),
            data.get('conversationId', ''), 'group' if group else 'private',
            (data.get('text') or {}).get('content', '').strip(),
            mentioned=bool(data.get('isInAtList')), unsupported=data.get('msgtype') != 'text')
    if provider == 'feishu':
        event = data['event']
        msg = event['message']
        content = json.loads(msg.get('content') or '{}')
        text = content.get('text', '')
        mentioned = False
        for mention in msg.get('mentions') or []:
            if bot_id and (mention.get('id') or {}).get('open_id') == bot_id:
                mentioned = True
                text = text.replace(mention.get('key') or '\0', '').strip()
        return InboundMessage(msg.get('message_id', ''),
            ((event.get('sender') or {}).get('sender_id') or {}).get('open_id', ''),
            msg.get('chat_id', ''), 'private' if msg.get('chat_type') == 'p2p' else 'group',
            text, mentioned=mentioned, unsupported=msg.get('message_type') != 'text')
    if provider == 'wechat':
        items = data.get('item_list') or []
        return InboundMessage(str(data.get('message_id') or ''), data.get('from_user_id', ''),
            data.get('group_id') or data.get('from_user_id', ''),
            'group' if data.get('group_id') else 'private',
            '\n'.join((item.get('text_item') or {}).get('text', '') for item in items if item.get('type') == 1),
            reply_context={'context_token': data.get('context_token', '')},
            unsupported=not items or any(item.get('type') != 1 for item in items))
    raise ValueError('Unsupported channel provider')
