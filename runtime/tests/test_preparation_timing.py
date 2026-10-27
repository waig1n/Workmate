"""Timing never exposes request contents or changes cancellation semantics."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from workmate_runtime.observability.preparation import preparation_stage

OWNER = SimpleNamespace(session_id='session', task_id='task', worker_id='worker', epoch=2)


@pytest.fixture(autouse=True)
def capture_module_logger(caplog):
    from workmate_runtime.observability.preparation import logger
    logger.addHandler(caplog.handler)
    try:yield
    finally:logger.removeHandler(caplog.handler)


def test_success_uses_monotonic_elapsed_and_safe_identifiers(caplog, monkeypatch):
    monkeypatch.setattr('workmate_runtime.observability.preparation.time.monotonic', iter([10, 10.125]).__next__)
    with caplog.at_level(logging.INFO), preparation_stage(OWNER, 'workspace', resume=False):
        pass
    record = caplog.records[-1]
    assert record.stage == 'workspace'
    assert record.duration_ms == 125
    assert record.task_id == 'task' and record.session_id == 'session'
    assert record.outcome == 'completed' and record.resume is False
    assert 'duration_ms=125.000' in record.message


@pytest.mark.parametrize('failure', [ValueError('secret prompt'), asyncio.CancelledError('secret token')])
def test_failure_and_cancel_propagate_without_logging_content(caplog, failure):
    with caplog.at_level(logging.INFO), pytest.raises(type(failure)) as raised:
        with preparation_stage(OWNER, 'total', resume=True):
            raise failure
    assert raised.value is failure
    record = caplog.records[-1]
    assert record.outcome == 'failed' and record.error_type == type(failure).__name__
    assert 'secret' not in caplog.text
    assert record.exc_info is None


def test_default_worker_logging_emits_once_without_root_info():
    import os,subprocess,sys
    code='''
import logging
from types import SimpleNamespace
from workmate_runtime.observability.preparation import preparation_stage
logging.getLogger().setLevel(logging.WARNING)
owner=SimpleNamespace(task_id='t',session_id='s',worker_id='w')
for i in range(2):
 with preparation_stage(owner,'workspace',resume=False):pass
'''
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,check=True,env=os.environ.copy())
    assert result.stderr.count('Runtime preparation')==2


def test_bootstrap_stage_emits_safe_workspace_identity(caplog):
    from workmate_runtime.observability.preparation import bootstrap_stage
    with pytest.raises(ValueError),bootstrap_stage('workspace-1','cold_start'):
        raise ValueError('secret connection string')
    record=caplog.records[-1]
    assert record.workspace_id=='workspace-1' and record.stage=='cold_start'
    assert record.error_type=='ValueError' and 'secret' not in record.getMessage()


def test_bootstrap_cancel_preserves_identity_and_monotonic(caplog,monkeypatch):
    from workmate_runtime.observability.preparation import bootstrap_stage
    monkeypatch.setattr('workmate_runtime.observability.preparation.time.monotonic',iter([4,4.25]).__next__)
    failure=asyncio.CancelledError('secret')
    with pytest.raises(asyncio.CancelledError) as raised:
        with bootstrap_stage('ws','verify'):raise failure
    assert raised.value is failure
    record=caplog.records[-1]
    assert record.duration_ms==250 and record.error_type=='CancelledError'
    assert 'secret' not in record.getMessage() and record.exc_info is None


def test_sink_follows_stderr_replacement_even_if_previous_stream_closed():
    import io
    from contextlib import redirect_stderr
    from workmate_runtime.observability.preparation import bootstrap_stage,logger
    first=io.StringIO();second=io.StringIO()
    with redirect_stderr(first):
        with bootstrap_stage('ws','first'):pass
    first.close()
    with redirect_stderr(second):
        with bootstrap_stage('ws','second'):pass
    assert second.getvalue().count('stage=second')==1
    assert sum(bool(getattr(handler,'_preparation_sink',False)) for handler in logger.handlers)==1
