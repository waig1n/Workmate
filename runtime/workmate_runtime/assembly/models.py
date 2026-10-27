"""Resolve a selected deployment profile directly into an AgentScope Core model."""
import os

from agentscope.credential import OpenAICredential, DashScopeCredential
from agentscope.model import OpenAIChatModel, DashScopeChatModel

from workmate_runtime.model_cards import _build_card, _synthetic_card_yaml


class ModelUnavailable(ValueError):
    """Selected profile cannot execute; messages must never include secrets."""


def build_model(settings, profile_name):
    """The selected profile is Product-owned; credentials stay in Worker memory.

    No App credential IDs or caller-supplied model/config dictionaries are
    accepted. Resolve each new assembly afresh so key rotation takes effect.
    Builtin media cards retain precedence, as in the existing product adapter.
    """
    profile = settings.chat_model_profiles.get(profile_name)
    if profile is None:
        raise ModelUnavailable('Selected model profile is unavailable')
    key = os.getenv(profile.api_key_env, '')
    if not key.strip():
        raise ModelUnavailable('Selected model credential is unavailable')
    credential_cls, model_cls = {
        'openai':(OpenAICredential,OpenAIChatModel),
        'dashscope':(DashScopeCredential,DashScopeChatModel),
    }[profile.model_type]
    values = {'api_key':key}
    endpoint = os.getenv(profile.base_url_env) if profile.base_url_env else profile.base_url
    if profile.base_url_env and not (endpoint or '').strip():
        raise ModelUnavailable('Selected model endpoint reference is unavailable')
    if endpoint:
        values['base_url'] = endpoint
    credential = credential_cls(**values)
    card = next((card for card in model_cls.list_models() if card.name == profile.model),None)
    if card is None:
        card = _build_card(model_cls.Parameters,_synthetic_card_yaml(profile))
    model = model_cls(credential=credential,model=profile.model,context_size=card.context_size)
    model.formatter.input_types = list(card.input_types)
    return model


def build_selected_model(settings, selected):
    """Reject deployment descriptor drift while permitting secret rotation."""
    from workmate_product.application.execution_context import SelectedModel
    selected = SelectedModel.model_validate(selected)
    profile = settings.chat_model_profiles.get(selected.profile_name)
    if profile is None:
        raise ModelUnavailable('Selected model profile is unavailable')
    current = SelectedModel(profile_name=profile.name,**profile.model_dump(exclude={'name','base_url'}))
    if current != selected:
        raise ModelUnavailable('Deployment model descriptor differs from admitted execution')
    return build_model(settings,selected.profile_name)
