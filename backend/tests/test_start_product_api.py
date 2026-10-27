# -*- coding: utf-8 -*-
"""Safe product-api startup migration gate tests."""

import pytest

from scripts import start_product_api


def test_upgrade_advances_supported_revision(monkeypatch):
    calls = []
    monkeypatch.setattr(
        start_product_api.subprocess, "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    start_product_api._upgrade("/app/alembic.ini")
    assert calls[0][0][-2:] == ["upgrade", "head"]
    assert calls[0][1]["check"] is True


def test_failed_upgrade_propagates(monkeypatch):
    import subprocess
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(start_product_api.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        start_product_api._upgrade("alembic.ini")


def test_main_migrates_before_starting_native_entry(monkeypatch,tmp_path):
    config=tmp_path/'alembic.ini'
    config.touch()
    calls=[]
    monkeypatch.setenv('WORKMATE_ALEMBIC_CONFIG',str(config))
    monkeypatch.setattr(start_product_api,'_upgrade',lambda value:calls.append(('migrate',value)))
    monkeypatch.setattr(start_product_api.os,'execvp',lambda name,args:calls.append(('exec',args)))
    start_product_api.main()
    assert calls[0]==('migrate',str(config))
    assert calls[1][1][1]=='app.main:app'


def test_migration_failure_never_starts_native_api(monkeypatch,tmp_path):
    config=tmp_path/'alembic.ini'
    config.touch()
    monkeypatch.setenv('WORKMATE_ALEMBIC_CONFIG',str(config))
    def fail(_):
        raise RuntimeError('migration failed')
    monkeypatch.setattr(start_product_api,'_upgrade',fail)
    monkeypatch.setattr(start_product_api.os,'execvp',lambda *_:pytest.fail('API started before successful migration'))
    with pytest.raises(RuntimeError,match='migration failed'):
        start_product_api.main()
