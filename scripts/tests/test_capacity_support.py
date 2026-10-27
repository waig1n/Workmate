"""Fixture limits and settings must not replace production S3 configuration."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import shlex
import sys

import pytest

ROOT=Path(__file__).parents[1]/'reliability/capacity_support'


def common():
    spec=importlib.util.spec_from_file_location('capacity_common_test',ROOT/'common.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_text_emission_parameters_are_fixed(monkeypatch):
    module=common(); monkeypatch.delenv('CAPACITY_MODE',raising=False)
    assert module.parameters()==(600,.02)
    monkeypatch.setenv('CAPACITY_MODE','smoke'); assert module.parameters()==(600,.02)
    assert module.chunk('key',7)=='key:000007:'+'x'*24


@pytest.mark.parametrize('kind',['text','file','file20','boundary','skill','mcp','rag','approval','stop'])
def test_latest_fixture_case_is_selected(kind):
    module=common(); key='a'*32
    messages=[SimpleNamespace(content=[SimpleNamespace(text='fixture-case:text '+'b'*32)]),
              SimpleNamespace(content=[SimpleNamespace(text='fixture-case:'+kind+' '+key)])]
    assert module.fixture_case(messages,'fallback')==(kind,key)


@pytest.mark.parametrize('size',[1048576,20971520])
def test_file_fixture_generates_exact_bytes_without_model_context(size,tmp_path):
    module=common(); key='a'*32
    command=module.file_command(key,size)
    assert len(command)<700
    code=shlex.split(command)[-1].replace('/workspace/output',str(tmp_path))
    exec(compile(code,'fixture','exec'),{})
    data=(tmp_path/(key+'.txt')).read_bytes()
    assert len(data)==size and data==((key+'\n').encode()*((size//33)+1))[:size]


def test_stop_marker_precedes_sleep_and_completion_side_effect():
    command=common().file_command('a'*32,1048576,stop=True)
    assert command.index('.started')<command.index('time.sleep(30)')<command.index('.completed')


@pytest.mark.parametrize(('key','size'),[('injection;cmd',1048576),('a'*32,0),('a'*32,20971521)])
def test_file_fixture_rejects_unbounded_inputs(key,size):
    with pytest.raises(ValueError): common().file_command(key,size)


def test_settings_preserve_s3_and_real_role_configuration(monkeypatch):
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.roles.settings import WorkerRoleSettings
    import workmate_runtime.roles.settings as role_module
    role=WorkerRoleSettings(runtime=RuntimeSettings(artifact_storage={'backend':'s3'},
        artifact_s3_endpoint='http://private-s3:9000',artifact_s3_bucket='artifacts',
        skill_s3_endpoint='http://private-s3:9000',skill_s3_bucket='skills',
        coordination_redis_url='redis://private-redis:6379',workspace_basedir='/shared/workspaces'),
        database_url='postgresql+asyncpg://test:test@pg/test',storage_node_id='node',
        workspace_dir='/shared/workspaces',artifact_dir='/shared/artifacts')
    monkeypatch.setattr(role_module,'load_worker_role_settings',lambda:role)
    monkeypatch.setenv('WORKMATE_LOAD_NAMESPACE','capacity-isolated'); monkeypatch.setenv('CAPACITY_CAPACITY','4')
    actual=common().settings()
    assert actual.runtime.artifact_s3_endpoint==role.runtime.artifact_s3_endpoint
    assert actual.runtime.skill_s3_bucket=='skills' and actual.runtime.worker.capacity==4
    assert actual.runtime.coordination_namespace=='capacity-isolated' and actual.runtime.reme_enabled is False
    assert actual.runtime.workspace_lifecycle.max_warm_per_worker==2
    assert actual.database_url==role.database_url


def test_fixture_configuration_preserves_supplied_storage_policy(monkeypatch,tmp_path):
    import yaml
    source=tmp_path/'input.yaml'
    source.write_text('product:\n  artifacts:\n    max_file_bytes: 52428800\n  models:\n    default: fixture\n')
    module=common(); monkeypatch.setattr(module,'EVIDENCE',tmp_path)
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(source))
    result=yaml.safe_load(Path(module.fixture_configuration()).read_text())
    assert result['product']['artifacts']['max_file_bytes']==52428800
    assert result['product']['models']['default']=='fixture'
    assert result['product']['knowledge']['embedding']['api_key_env']=='WORKMATE_LOAD_MODEL_KEY'


def supervisor(monkeypatch):
    monkeypatch.setitem(sys.modules,'common',common())
    spec=importlib.util.spec_from_file_location('capacity_sut_test',ROOT/'sut.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_supervisor_rejects_unplanned_worker_capacity(monkeypatch):
    sut=supervisor(monkeypatch)
    assert sut.arguments(['--workers','2','--capacity','4']).capacity==4
    for argv in (['--workers','3','--capacity','2'],['--workers','1','--capacity','8']):
        with pytest.raises(SystemExit): sut.arguments(argv)


@pytest.mark.asyncio
@pytest.mark.parametrize('hang',[False,True])
async def test_supervisor_bounds_shutdown_and_reports_forced_kill(monkeypatch,hang):
    import asyncio
    sut=supervisor(monkeypatch)
    class Process:
        returncode=None
        killed=False
        terminated=False
        def terminate(self): self.terminated=True
        def kill(self): self.killed=True; self.returncode=-9
        async def wait(self):
            if hang and not self.killed: await asyncio.Event().wait()
            if not self.killed: self.returncode=0
            return self.returncode
    process=Process()
    code,killed=await sut.stop_child(process,.01)
    assert process.terminated
    assert (code,killed)==((-9,True) if hang else (0,False))


def test_stop_injection_leaves_started_but_not_completion_effect(monkeypatch,tmp_path,capsys):
    import asyncio
    module=common(); key='a'*32
    code=shlex.split(module.file_command(key,1048576,stop=True))[-1].replace('/workspace/output',str(tmp_path))
    def interrupted_sleep(seconds):
        assert seconds==30
        raise asyncio.CancelledError()
    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules,'time',SimpleNamespace(sleep=interrupted_sleep))
        with pytest.raises(asyncio.CancelledError): exec(compile(code,'fixture','exec'),{})
    assert 'CAPACITY_STOP_STARTED:'+key in capsys.readouterr().out
    assert module.stop_effects(key,tmp_path)==dict(key=key,started_count=1,completed_exists=False,result_exists=False)
    (tmp_path/(key+'.completed')).write_text('unexpected')
    assert module.stop_effects(key,tmp_path)['completed_exists'] is True


def test_generated_fixture_configuration_loads_real_runtime_settings(monkeypatch,tmp_path):
    from workmate_runtime.config import load_runtime_settings
    module=common(); monkeypatch.setattr(module,'EVIDENCE',tmp_path)
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(ROOT.parent/'load_support/config.yaml'))
    monkeypatch.setenv('WORKMATE_LOAD_MODEL_KEY','local-fixture-only')
    for name,value in {'KNOWLEDGE_QDRANT_URL':'http://127.0.0.1:9','KNOWLEDGE_QDRANT_API_KEY':'fixture','KNOWLEDGE_S3_ENDPOINT':'http://rustfs:9000','KNOWLEDGE_S3_BUCKET':'capacity-knowledge','KNOWLEDGE_S3_ACCESS_KEY':'fixture','KNOWLEDGE_S3_SECRET_KEY':'fixture'}.items():
        monkeypatch.setenv(name,value)
    generated=module.fixture_configuration()
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',generated)
    runtime=load_runtime_settings()
    assert runtime.knowledge.enabled is True
    assert runtime.knowledge_embedding_model_dimensions==2
    assert runtime.knowledge_embedding_model_dimensions_mode=='send'


@pytest.mark.asyncio
async def test_fixture_stop_marker_endpoint_requires_jwt_and_valid_key(monkeypatch,tmp_path):
    import time
    import jwt
    from fastapi import FastAPI
    from httpx import AsyncClient,ASGITransport
    module=common();monkeypatch.setitem(sys.modules,'common',module)
    spec=importlib.util.spec_from_file_location('capacity_gateway_test',ROOT/'gateway.py')
    gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)
    app=FastAPI();gateway.install_stop_probe(app,root=tmp_path)
    token=jwt.encode({'sub':'fixture-user','ver':0,'exp':int(time.time())+60},module.JWT_SECRET,algorithm='HS256')
    headers={'Authorization':'Bearer '+token};key='a'*32
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://fixture') as client:
        assert (await client.get('/__capacity/stop/'+key)).status_code==401
        assert (await client.get('/__capacity/stop/'+key,headers={'Authorization':'Bearer bad'})).status_code==401
        assert (await client.get('/__capacity/stop/invalid',headers=headers)).status_code==422
        assert (await client.get('/__capacity/stop/'+key,headers=headers)).json()==dict(key=key,started_count=0,completed_exists=False,result_exists=False)
        (tmp_path/(key+'.started')).write_text('started')
        value=(await client.get('/__capacity/stop/'+key,headers=headers)).json()
        assert value==dict(key=key,started_count=1,completed_exists=False,result_exists=False)
        (tmp_path/(key+'.completed')).write_text('unexpected')
        assert (await client.get('/__capacity/stop/'+key,headers=headers)).json()['completed_exists'] is True
