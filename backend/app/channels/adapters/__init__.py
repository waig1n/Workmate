"""Platform factories. Credentials and raw transport exceptions remain private."""
from .dingtalk import DingtalkAdapter
from .feishu import FeishuAdapter
from .wechat import WechatAdapter
from .wecom import WecomAdapter


def create_adapter(provider: str, credentials: dict, *, cursor='', persist_cursor=None):
    if provider == 'wechat':
        return WechatAdapter(credentials, cursor=cursor, persist_cursor=persist_cursor)
    adapter = {'wecom': WecomAdapter, 'feishu': FeishuAdapter, 'dingtalk': DingtalkAdapter}.get(provider)
    if adapter is None:
        raise ValueError('Unsupported channel provider')
    return adapter(credentials)
