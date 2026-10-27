"""CAPACITY fixture policy; real production settings and S3 adapters remain intact."""
import importlib
import json
import os
from pathlib import Path
import re
import shlex

JWT_SECRET='fixture-load-only-key-not-a-real-secret-000000'
EVIDENCE=Path(os.environ.get('CAPACITY_EVIDENCE','/evidence'))


def write(name,value):
    EVIDENCE.mkdir(parents=True,exist_ok=True)
    (EVIDENCE/name).write_text(json.dumps(value,default=str,indent=2))


def guard(label):
    paths={}
    for name,prefix in {'app':'/source/backend/','workmate_runtime':'/source/runtime/',
        'workmate_product':'/source/packages/product/src/','workmate_contracts':'/source/packages/contracts/src/'}.items():
        path=Path(importlib.import_module(name).__file__).resolve()
        assert str(path).startswith(prefix) and path.suffix=='.py',(name,str(path))
        paths[name]=str(path)
    write(label+'-origins.json',paths)


def settings():
    from workmate_runtime.roles.settings import load_worker_role_settings
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.roles.settings import WorkerRoleSettings
    role=load_worker_role_settings()
    values=role.runtime.model_dump()
    values.update(reme_enabled=False,coordination_namespace=os.environ['WORKMATE_LOAD_NAMESPACE'])
    values['worker']['capacity']=int(os.environ.get('CAPACITY_CAPACITY','2'))
    values['workspace_lifecycle'].update(max_warm_per_worker=2,warm_ttl_seconds=30,sweep_interval_seconds=2)
    values['knowledge']['enabled']=True
    runtime=RuntimeSettings.model_validate(values)
    assert runtime.artifact_storage.backend=='s3','CAPACITY requires actual S3 artifacts'
    assert all((runtime.artifact_s3_endpoint,runtime.artifact_s3_bucket,runtime.skill_s3_endpoint,runtime.skill_s3_bucket))
    return WorkerRoleSettings(runtime=runtime,database_url=role.database_url,storage_node_id='node',
        workspace_dir=role.workspace_dir,artifact_dir=role.artifact_dir)


def chunk(key,index): return f'{key}:{index:06d}:'+'x'*24

def parameters(): return (600,.02)


def fixture_case(messages,fallback):
    for message in reversed(messages):
        for block in message.content if isinstance(message.content,list) else []:
            match=re.fullmatch(r'fixture-case:(text|file|file20|boundary|skill|mcp|rag|approval|stop) ([0-9a-f]{32})',getattr(block,'text',''))
            if match: return match[1],match[2]
    return 'text',fallback


def file_command(key,size,*,stop=False):
    if not re.fullmatch('[0-9a-f]{32}',key) or size not in (1048576,20971520):
        raise ValueError('Invalid CAPACITY file fixture')
    code=("from pathlib import Path; import time; key="+repr(key)+"; root=Path('/workspace/output'); root.mkdir(parents=True,exist_ok=True); ")
    if stop:
        code+="(root/(key+'.started')).write_text('started'); print('CAPACITY_STOP_STARTED:'+key,flush=True); time.sleep(30); (root/(key+'.completed')).write_text('unexpected completion'); "
    code+="size="+str(size)+"; pattern=(key+'\\n').encode(); (root/(key+'.txt')).write_bytes((pattern*((size//len(pattern))+1))[:size])"
    return 'CAPACITY_FILE_KEY='+key+' python3 -c '+shlex.quote(code)


def fixture_configuration():
    """Keep supplied policy, add only the declared deterministic RAG fixture."""
    import yaml
    source=Path(os.environ['WORKMATE_CONFIG_FILE'])
    values=yaml.safe_load(source.read_text()) or {}
    product=values.setdefault('product',{})
    knowledge=product.setdefault('knowledge',{})
    knowledge['enabled']=True
    knowledge['embedding']={'type':'openai','model':'fixture','dimensions':2,'dimensions_mode':'send','api_key_env':'WORKMATE_LOAD_MODEL_KEY'}
    destination=EVIDENCE/'capacity-config.yaml'
    destination.write_text(yaml.safe_dump(values,sort_keys=False))
    return str(destination)


def stop_effects(key,root):
    if not re.fullmatch('[0-9a-f]{32}',key): raise ValueError('Invalid Stop fixture key')
    markers=list(Path(root).rglob(key+'.started'))
    return {'key':key,'started_count':len(markers),
        'completed_exists':any(path.with_name(key+'.completed').exists() for path in markers),
        'result_exists':any(path.with_name(key+'.txt').exists() for path in markers)}
