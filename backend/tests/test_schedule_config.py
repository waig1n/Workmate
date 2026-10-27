from pathlib import Path

import pytest
import yaml

from app.config import Settings
from workmate_product.config.schedules import SchedulePolicy


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / 'workmate.yaml'
    path.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    for key in ('ENABLED','POLL_INTERVAL_SECS','BATCH_SIZE','MISFIRE_GRACE_SECS'):
        monkeypatch.delenv('SCHEDULE_'+key, raising=False)
    return path


def test_schedule_defaults():
    assert SchedulePolicy().model_dump() == dict(enabled=True, poll_interval_secs=5, batch_size=20, misfire_grace_secs=300)
    settings = Settings(_env_file=None)
    assert settings.schedule_enabled and settings.schedule_misfire_grace_secs == 300


@pytest.mark.parametrize('field,yaml_value,env_value,expected', [
    ('enabled',False,'true',True), ('poll_interval_secs',3,'10',10),
    ('batch_size',7,'4',4), ('misfire_grace_secs',90,'60',60),
])
def test_yaml_and_environment_precedence(isolated_config,monkeypatch,field,yaml_value,env_value,expected):
    isolated_config.write_text(yaml.safe_dump({'product':{'schedules':{field:yaml_value}}}))
    assert getattr(Settings(_env_file=None),'schedule_'+field) == yaml_value
    monkeypatch.setenv('SCHEDULE_'+field.upper(),env_value)
    assert getattr(Settings(_env_file=None),'schedule_'+field) == expected


@pytest.mark.parametrize('field,value', [('poll_interval_secs',0),('batch_size',0),('batch_size',101),('misfire_grace_secs',-1),('enabled','invalid')])
def test_invalid_yaml_and_environment_fail(isolated_config,monkeypatch,field,value):
    isolated_config.write_text(yaml.safe_dump({'product':{'schedules':{field:value}}}))
    with pytest.raises(ValueError): Settings(_env_file=None)
    isolated_config.write_text('version: 1\n')
    monkeypatch.setenv('SCHEDULE_'+field.upper(),str(value))
    with pytest.raises(ValueError): Settings(_env_file=None)


def test_shipped_yaml_matches_policy_defaults():
    config = yaml.safe_load((Path(__file__).parents[2] / 'config/workmate.yaml').read_text())
    assert config['product']['schedules'] == SchedulePolicy().model_dump()
