"""Connection observers must not inspect coroutine internals in greenlet callbacks."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


class FakeTask:
    def get_name(self):
        return 'safe-task-label'

    def cancelling(self):
        return 2

    def get_coro(self):
        # The historical observer fails here before touching any real coroutine
        # or frame, so its regression test cannot reproduce the native crash.
        raise AssertionError('Unsafe coroutine introspection in pool callback')


@pytest.fixture
def observer(monkeypatch, tmp_path):
    override=os.environ.get('FAULT_DIAGNOSTICS_MODULE')
    path=Path(override) if override else Path(__file__).parents[1]/'reliability/database_fault_support/diagnostics.py'
    spec=importlib.util.spec_from_file_location('isolated_diagnostics_test',path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    callbacks={}
    def listens_for(target, name):
        assert target in (module.Pool,module.Engine)
        def register(function):
            assert name not in callbacks
            callbacks[name]=function
            return function
        return register
    # Do not register listeners on SQLAlchemy's process-global Engine or Pool.
    monkeypatch.setattr(module,'event',SimpleNamespace(listens_for=listens_for))
    monkeypatch.setattr(module,'asyncio',SimpleNamespace(current_task=lambda:FakeTask()))
    def destination(name):
        assert name in ('/evidence/pg-connection-trace.jsonl','/evidence/pg-connection-summary.json')
        return tmp_path/Path(name).name
    monkeypatch.setattr(module,'Path',destination)
    flush=module.install()
    return module,callbacks,flush,tmp_path


def test_observer_uses_only_task_name_and_cancelling_without_coroutine_access(observer):
    module,callbacks,flush,path=observer
    driver=object(); dbapi=SimpleNamespace(driver_connection=driver); record=object()
    callbacks['connect'](dbapi,record)
    callbacks['checkout'](dbapi,record,object())
    callbacks['checkin'](dbapi,record)
    callbacks['reset'](dbapi,record,SimpleNamespace(terminate_only=False,asyncio_safe=True,transaction_was_reset=True))
    callbacks['reset'](dbapi,record,SimpleNamespace(terminate_only=True,asyncio_safe=False,transaction_was_reset=False))
    callbacks['invalidate'](dbapi,record,TimeoutError('must not log raw error details'))
    callbacks['close'](dbapi,record)
    callbacks['handle_error'](SimpleNamespace(original_exception=ConnectionError('private SQL detail'),is_disconnect=True))
    flush()
    summary=json.loads((path/'pg-connection-summary.json').read_text())
    assert summary['counts']=={'connect':1,'checkout':1,'checkin':1,'reset':2,'invalidate':1,'close':1,'sql_error':1}
    assert all(row['task']=='safe-task-label' and row['cancel_count']==2 for row in summary['recent'])
    assert all('await_chain' not in row for row in summary['recent'])
    trace=[json.loads(line) for line in (path/'pg-connection-trace.jsonl').read_text().splitlines()]
    assert [row['event'] for row in trace]==['reset','invalidate','close','sql_error']
    assert trace[0]['last_checkout']['record_id']==hex(id(record))
    assert trace[0]['driver_id']==hex(id(driver))
    assert trace[-1]['is_disconnect'] is True and trace[-1]['error_type']=='ConnectionError'
    text=json.dumps(summary)+json.dumps(trace)
    assert 'private SQL detail' not in text and 'must not log raw error details' not in text


@pytest.mark.parametrize('no_loop',[False,True])
def test_observer_handles_absent_task_and_bounds_recent_buffer(observer,monkeypatch,no_loop):
    module,callbacks,flush,path=observer
    def current_task():
        if no_loop: raise RuntimeError('no running event loop')
        return None
    monkeypatch.setattr(module,'asyncio',SimpleNamespace(current_task=current_task))
    record=object()
    for _ in range(270): callbacks['checkout'](None,record,None)
    flush()
    summary=json.loads((path/'pg-connection-summary.json').read_text())
    assert summary['counts']=={'checkout':270}
    assert len(summary['recent'])==256 and len(summary['last_checkout'])==1
    assert all(row['task'] is None and row['cancel_count'] is None for row in summary['recent'])
    assert all(row['driver_id'] is None and row['dbapi_id'] is None for row in summary['recent'])
