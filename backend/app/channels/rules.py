"""Validated public configuration and audience-aware text rendering."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Provider = Literal['wecom', 'feishu', 'dingtalk', 'wechat']


class ChannelCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    provider: Provider
    agent_id: str = Field(min_length=1, max_length=64)
    credentials: dict[str, str] = Field(default_factory=dict)
    group_enabled: bool = False
    group_allowlist: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def groups(self):
        if self.group_enabled and (self.provider == 'wechat' or not self.group_allowlist):
            raise ValueError('Explicit allowed enterprise groups required')
        if any(not value.strip() or len(value) > 256 for value in self.group_allowlist):
            raise ValueError('Invalid group identifier')
        return self


class ChannelUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=100)
    agent_id: str = Field(min_length=1, max_length=64)
    enabled: bool
    group_enabled: bool = False
    group_allowlist: list[str] = Field(default_factory=list, max_length=100)
    credentials: dict[str, str] | None = None


KEYS = {'wecom': ('bot_id','secret'), 'feishu': ('app_id','app_secret'),
        'dingtalk': ('client_id','client_secret'), 'wechat': ()}


def credentials_for(provider, values):
    keys = KEYS[provider]
    optional = {'card_template_id'} if provider == 'dingtalk' else set()
    if not set(keys) <= set(values) or set(values) - set(keys) - optional or any(not isinstance(values[k], str) or not values[k].strip()
                                      or len(values[k]) > 4096 for k in keys):
        raise ValueError('Invalid channel credentials')
    result = {key: values[key].strip() for key in keys}
    for key in optional & values.keys():
        value = values[key]
        if not isinstance(value, str) or len(value) > 256:
            raise ValueError('Invalid channel credentials')
        if value.strip():
            result[key] = value.strip()
    return result


def render_body(text, url):
    """Shared byte budget for live and final output; links only on truncation."""
    if len(text.encode('utf-8')) <= 3500:
        return text
    suffix = '\n查看完整回答：' + url
    budget = max(0, 3500 - len(suffix.encode('utf-8')))
    return text.encode('utf-8')[:budget].decode('utf-8', errors='ignore') + suffix


def render_reply(chat_type, status, assistant_text, url, *, has_artifacts=False):
    labels = {'created':'已受理，请在工作台查看进度。', 'running':'任务正在执行。',
              'awaiting_permission':'任务需要确认，请登录工作台处理。',
              'awaiting_external_result':'任务等待外部输入，请登录工作台处理。',
              'completed':'任务已完成。', 'failed':'任务未能完成，请在工作台查看。',
              'cancelled':'任务已停止。', 'interrupted':'任务已中断，请在工作台查看。'}
    body = labels.get(status, '请在工作台查看任务状态。')
    if chat_type == 'private' and status == 'completed':
        body = assistant_text if assistant_text.strip() else body
        if has_artifacts:
            body += '\n查看文件：' + url
        return render_body(body, url)
    needs_link = chat_type == 'group' or status in (
        'awaiting_permission', 'awaiting_external_result', 'failed', 'interrupted')
    return body + ('\n' + url if needs_link else '')
