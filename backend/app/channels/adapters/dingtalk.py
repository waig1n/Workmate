"""DingTalk Stream: durable callback before ACK; official REST outbound API."""
import json

from .base import safe_transport, Adapter, ChannelTransportError, MAX_RESPONSE, websocket_url
from .parsing import normalize


class DingtalkAdapter(Adapter):
    async def run(self, emit):
        import websockets
        from dingtalk_stream import CallbackMessage, ChatbotMessage

        data = await self.request('POST', 'https://api.dingtalk.com/v1.0/gateway/connections/open',
            json={'clientId': self._credentials['client_id'], 'clientSecret': self._credentials['client_secret'],
                'subscriptions': [{'type': 'CALLBACK', 'topic': ChatbotMessage.TOPIC}],
                'ua': 'workmate/2.0'})
        endpoint = websocket_url(data['endpoint'], 'dingtalk.com')
        from urllib.parse import urlencode
        endpoint += ('&' if '?' in endpoint else '?') + urlencode({'ticket': data['ticket']})
        async with websockets.connect(endpoint, max_size=MAX_RESPONSE, max_queue=16,
                open_timeout=20, close_timeout=5, proxy=None) as socket:
            self._socket = socket
            self.ready.set()
            async for raw in socket:
                frame = json.loads(raw)
                headers = frame.get('headers') or {}
                if frame.get('type') == 'CALLBACK' and headers.get('topic') == ChatbotMessage.TOPIC:
                    callback = CallbackMessage.from_dict(frame)
                    await emit(normalize('dingtalk', callback.data))
                elif frame.get('type') == 'SYSTEM' and headers.get('topic') == 'disconnect':
                    return
                await socket.send(json.dumps({'code': 200, 'headers': {'messageId': headers.get('messageId', ''),
                    'contentType': 'application/json'}, 'message': 'OK', 'data': frame.get('data', '{}') if frame.get('type') == 'SYSTEM' else json.dumps({'response': 'OK'})}))

    @safe_transport
    async def send(self, target, text):
        token = await self.request('POST', 'https://api.dingtalk.com/v1.0/oauth2/accessToken',
            json={'appKey': self._credentials['client_id'], 'appSecret': self._credentials['client_secret']})
        access_token = token.get('accessToken')
        if not access_token:
            raise ChannelTransportError('platform_authentication_failed')
        group = target['chat_type'] == 'group'
        path = '/v1.0/robot/groupMessages/send' if group else '/v1.0/robot/oToMessages/batchSend'
        body = {'robotCode': self._credentials['client_id'], 'msgKey': 'sampleText',
                'msgParam': json.dumps({'content': text}, ensure_ascii=False)}
        body.update({'openConversationId': target['chat_id']} if group else {'userIds': [target['sender_id']]})
        result = await self.request('POST', 'https://api.dingtalk.com' + path,
            headers={'x-acs-dingtalk-access-token': access_token}, json=body)
        self.check(result)
        if result.get('invalidStaffIdList') or result.get('flowControlledStaffIdList'):
            raise ChannelTransportError('platform_recipient_rejected')
        return result.get('processQueryKey')

    def supports_stream(self, target):
        return bool(self._credentials.get('card_template_id'))

    @safe_transport
    async def stream(self, target, text, *, stream_id, receipt=None, finish=False, sequence=1):
        import uuid

        template = self._credentials.get('card_template_id')
        if not template:
            raise ChannelTransportError('stream_template_missing')
        token = await self.request('POST', 'https://api.dingtalk.com/v1.0/oauth2/accessToken',
            json={'appKey': self._credentials['client_id'], 'appSecret': self._credentials['client_secret']})
        access_token = token.get('accessToken')
        if not access_token:
            raise ChannelTransportError('platform_authentication_failed')
        headers = {'x-acs-dingtalk-access-token': access_token}
        card_id = receipt or stream_id
        if not receipt:
            # The configured AI template must contain streaming field content.
            body = {'cardTemplateId': template, 'outTrackId': card_id,
                    'cardData': {'cardParamMap': {'content': text}}, 'callbackType': 'STREAM',
                    'imGroupOpenSpaceModel': {'supportForward': False},
                    'imRobotOpenSpaceModel': {'supportForward': False}}
            if target['chat_type'] == 'group':
                body.update({'openSpaceId': 'dtv1.card//IM_GROUP.' + target['chat_id'],
                             'imGroupOpenDeliverModel': {'robotCode': self._credentials['client_id']}})
            else:
                body.update({'openSpaceId': 'dtv1.card//IM_ROBOT.' + target['sender_id'],
                             'imRobotOpenDeliverModel': {'spaceType': 'IM_ROBOT'}})
            result = await self.request('POST', 'https://api.dingtalk.com/v1.0/card/instances/createAndDeliver',
                                        headers=headers, json=body)
            self.check(result)
            if result.get('success') is False:
                raise ChannelTransportError('platform_rejected_request')
        result = await self.request('PUT', 'https://api.dingtalk.com/v1.0/card/streaming', headers=headers,
            json={'outTrackId': card_id, 'guid': str(uuid.uuid5(uuid.NAMESPACE_URL,
                  f'{card_id}:{sequence}:{finish}:{text}')), 'key': 'content', 'content': text,
                  'isFull': True, 'isFinalize': finish, 'isError': False})
        self.check(result)
        if result.get('success') is False:
            raise ChannelTransportError('platform_rejected_request')
        return card_id
