"""Acceptance target validation runs before app imports or network access."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT=Path(__file__).with_name('native_acceptance_server.py')


def check(tmp_path, overrides):
    environment={key:value for key,value in os.environ.items()
        if key not in {'WORKMATE_ACCEPTANCE_REDIS_URL','WORKMATE_ACCEPTANCE_REDIS_NAMESPACE'}}
    # Even an inherited deployment URL must never authorize this local harness.
    environment['WORKMATE_COORDINATION_REDIS_URL']='redis://business.example:6379/0'
    environment.update(overrides)
    return subprocess.run([sys.executable,str(SCRIPT),'--check'],cwd=tmp_path,
        env=environment,text=True,capture_output=True,timeout=10)


def test_check_accepts_explicit_dedicated_target_without_loading_dotenv(tmp_path):
    (tmp_path/'.env').write_text('WORKMATE_COORDINATION_REDIS_URL=redis://business.example:6379/0\n')
    result=check(tmp_path,{'WORKMATE_ACCEPTANCE_REDIS_URL':'redis://127.0.0.1:55440/0',
        'WORKMATE_ACCEPTANCE_REDIS_NAMESPACE':'acceptance-browser'})
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout)
    assert value['redis']=='redis://127.0.0.1:55440/0'
    assert value['namespace']=='acceptance-browser'
    assert value['env_file'] is False and value['network_checked'] is False


@pytest.mark.parametrize('overrides',[
    {},
    {'WORKMATE_ACCEPTANCE_REDIS_URL':'redis://business.example:55440/0',
        'WORKMATE_ACCEPTANCE_REDIS_NAMESPACE':'acceptance-browser'},
    {'WORKMATE_ACCEPTANCE_REDIS_URL':'redis://127.0.0.1:6379/0',
        'WORKMATE_ACCEPTANCE_REDIS_NAMESPACE':'acceptance-browser'},
    {'WORKMATE_ACCEPTANCE_REDIS_URL':'redis://127.0.0.1:55440/0',
        'WORKMATE_ACCEPTANCE_REDIS_NAMESPACE':'workmate'},
    {'WORKMATE_ACCEPTANCE_REDIS_URL':'redis://127.0.0.1:55440/1',
        'WORKMATE_ACCEPTANCE_REDIS_NAMESPACE':'acceptance-browser'},
])
def test_check_rejects_missing_remote_or_deployment_targets(tmp_path,overrides):
    result=check(tmp_path,overrides)
    assert result.returncode!=0
    assert 'WORKMATE_ACCEPTANCE_REDIS_' in result.stderr
    assert not result.stdout
