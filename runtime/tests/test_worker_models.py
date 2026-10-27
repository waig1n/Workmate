"""Direct Core model assembly retains profile media and credential boundaries."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from workmate_runtime.config import RuntimeSettings, ChatModelProfile
from workmate_runtime.assembly.models import build_model, ModelUnavailable


def settings(provider='openai', **changes):
    profile = ChatModelProfile(name='chosen',model_type=provider,model='private-model',
        api_key_env='WORKMATE_FIXTURE_MODEL_KEY',context_size=12345,
        model_input_types=['text/plain','image/png'],**changes)
    return RuntimeSettings(chat_model_profiles={'chosen':profile},default_chat_profile='chosen')


@pytest.mark.parametrize('provider',['openai','dashscope'])
def test_direct_profile_model_without_service_credentials(monkeypatch,provider):
    monkeypatch.setenv('WORKMATE_FIXTURE_MODEL_KEY','fixture-only')
    model = build_model(settings(provider),'chosen')
    assert model.model == 'private-model' and model.context_size == 12345
    assert model.formatter.input_types == ['text/plain','image/png']
    assert model.credential.api_key.get_secret_value() == 'fixture-only'


def test_missing_profile_or_key_is_closed_without_secret_in_error(monkeypatch):
    monkeypatch.delenv('WORKMATE_FIXTURE_MODEL_KEY',raising=False)
    for name in ('chosen','absent',None):
        with pytest.raises(ModelUnavailable):
            build_model(settings(),name)


def test_profile_key_is_resolved_again_after_rotation(monkeypatch):
    monkeypatch.setenv('WORKMATE_FIXTURE_MODEL_KEY','first-fixture')
    first = build_model(settings(),'chosen')
    monkeypatch.setenv('WORKMATE_FIXTURE_MODEL_KEY','second-fixture')
    second = build_model(settings(),'chosen')
    assert first.credential.api_key != second.credential.api_key


def test_endpoint_reference_is_resolved_again_without_persisting_url(monkeypatch):
    monkeypatch.setenv('WORKMATE_FIXTURE_MODEL_KEY','fixture-only')
    monkeypatch.setenv('WORKMATE_FIXTURE_MODEL_URL','https://first.invalid/v1')
    selected = settings(base_url_env='WORKMATE_FIXTURE_MODEL_URL')
    first = build_model(selected,'chosen')
    monkeypatch.setenv('WORKMATE_FIXTURE_MODEL_URL','https://second.invalid/v1')
    second = build_model(selected,'chosen')
    assert first.credential.base_url == 'https://first.invalid/v1'
    assert second.credential.base_url == 'https://second.invalid/v1'
    monkeypatch.delenv('WORKMATE_FIXTURE_MODEL_URL')
    with pytest.raises(ModelUnavailable):
        build_model(selected,'chosen')


def test_admitted_descriptor_blocks_profile_drift_but_allows_key_rotation(monkeypatch):
    from workmate_product.application.execution_context import SelectedModel
    from workmate_runtime.assembly.models import build_selected_model
    monkeypatch.setenv('WORKMATE_FIXTURE_MODEL_KEY','fixture-only')
    config = settings()
    profile = config.chat_model_profiles['chosen']
    selected = SelectedModel(profile_name=profile.name,**profile.model_dump(exclude={'name','base_url'}))
    assert build_selected_model(config,selected).model == 'private-model'
    monkeypatch.setenv('WORKMATE_FIXTURE_MODEL_KEY','rotated-fixture')
    assert build_selected_model(config,selected).credential.api_key.get_secret_value() == 'rotated-fixture'
    profile.model = 'new-model'
    with pytest.raises(ModelUnavailable):
        build_selected_model(config,selected)


def test_builtin_card_wins_and_gateway_is_from_selected_profile(monkeypatch):
    from agentscope.model import OpenAIChatModel
    monkeypatch.setenv('WORKMATE_FIXTURE_MODEL_KEY','fixture-only')
    selected = settings(base_url='https://fixture.invalid/v1')
    selected.chat_model_profiles['chosen'].model = 'gpt-4o-mini'
    expected = next(c for c in OpenAIChatModel.list_models() if c.name == 'gpt-4o-mini')
    model = build_model(selected,'chosen')
    assert model.context_size == expected.context_size
    assert model.formatter.input_types == expected.input_types
    assert model.credential.base_url == 'https://fixture.invalid/v1'


def test_model_assembly_does_not_import_app_or_product_routes():
    root = Path(__file__).resolve().parents[2]
    source = '''
import sys, os
from importlib.abc import MetaPathFinder
class Block(MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if name=='fastapi' or name=='app' or name.startswith(('app.','agentscope.app')):
            raise AssertionError('Forbidden dependency: '+name)
sys.meta_path.insert(0,Block())
from workmate_runtime.assembly.models import build_model
from workmate_runtime.config import RuntimeSettings, ChatModelProfile
os.environ['WORKMATE_FIXTURE_MODEL_KEY']='fixture-only'
p=ChatModelProfile(name='fixture',model_type='openai',model='private-fixture',api_key_env='WORKMATE_FIXTURE_MODEL_KEY')
build_model(RuntimeSettings(chat_model_profiles={'fixture':p}),'fixture')
'''
    result = subprocess.run([sys.executable,'-c',source],capture_output=True,text=True,timeout=20,
        env={**os.environ,'PYTHONPATH':os.pathsep.join(str(root/p) for p in
            ('runtime','packages/product/src','packages/contracts/src'))})
    assert result.returncode == 0,result.stderr
