"""Gateway and Worker share bounded log policy without enabling live streaming."""
import pytest

from app.config import Settings
from app.config_file import _load_file_config_path
from workmate_runtime.config import load_runtime_settings
from workmate_product.config.realtime import RealtimePolicy, realtime_overrides

LOADERS=(load_runtime_settings,lambda:Settings(_env_file=None))
DEFAULTS = dict(retention_seconds=3600, max_log_bytes=8388608, ttl_seconds=7200,
    snapshot_interval_seconds=5.0, snapshot_max_bytes=65536, max_buffer_bytes=2097152,
    max_buffer_events=256, operation_timeout_seconds=2.0, outage_grace_seconds=30.0,
    reader_page_events=100, client_buffer_events=256, client_buffer_bytes=2097152, reader_block_ms=1000,
    reset_max_rows=10000, reset_max_bytes=8388608,
    authorize_interval_seconds=15.0, heartbeat_interval_seconds=15.0, reset_retry_seconds=2.0,
    max_gateway_readers=1024, max_gateway_subscriptions=4096)
COMPONENT_FIELDS = {
    'snapshot_interval_seconds': (0.1, 60, 1.25),
    'snapshot_max_bytes': (1024, 8388608, 32768),
    'max_buffer_bytes': (266240, 67108864, 1048576),
    'max_buffer_events': (16, 8192, 512),
    'operation_timeout_seconds': (0.1, 30, 1.5),
    'outage_grace_seconds': (1, 300, 10.5),
    'reader_page_events': (1, 1000, 200),
    'client_buffer_events': (16, 8192, 512),
    'client_buffer_bytes': (266240, 67108864, 1048576),
    'reader_block_ms': (1, 60000, 500),
    'reset_max_rows': (1, 100000, 5000),
    'reset_max_bytes': (266240, 67108864, 4194304),
    'authorize_interval_seconds': (0.1, 60, 5.5),
    'heartbeat_interval_seconds': (0.1, 30, 10.5),
    'reset_retry_seconds': (0.1, 30, 1.25),
    'max_gateway_readers': (1, 16384, 512),
    'max_gateway_subscriptions': (1, 65536, 2048),
}


@pytest.fixture
def config(tmp_path,monkeypatch):
    path=tmp_path/'realtime.yaml'
    path.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(path))
    for field in RealtimePolicy.model_fields:
        for key in ('WORKMATE_REALTIME_'+field.upper(), 'workmate_realtime_'+field):
            monkeypatch.delenv(key,raising=False)
    _load_file_config_path.cache_clear()
    yield path
    _load_file_config_path.cache_clear()


def write_policy(config,body):
    config.write_text('version: 1\nrealtime:\n'+body)
    _load_file_config_path.cache_clear()


def test_defaults_agree_across_roles(config):
    assert RealtimePolicy().model_dump() == DEFAULTS
    for load in LOADERS:
        assert load().realtime==RealtimePolicy()


def test_shared_yaml_policy(config):
    write_policy(config,'  retention_seconds: 120\n  max_log_bytes: 524288\n  ttl_seconds: 240\n')
    for load in LOADERS:
        assert load().realtime.model_dump() == {**DEFAULTS, 'retention_seconds':120, 'max_log_bytes':524288, 'ttl_seconds':240}


@pytest.mark.parametrize('field,value',[('retention_seconds',600),('max_log_bytes',1048576),('ttl_seconds',9000)])
def test_environment_overrides_yaml_for_both_roles(config,monkeypatch,field,value):
    write_policy(config,'  retention_seconds: 120\n  max_log_bytes: 524288\n  ttl_seconds: 7200\n')
    monkeypatch.setenv('WORKMATE_REALTIME_'+field.upper(),str(value))
    for load in LOADERS:
        policy=load().realtime
        assert getattr(policy,field)==value
        if field!='retention_seconds':
            assert policy.retention_seconds==120


@pytest.mark.parametrize('field,value',[
    ('retention_seconds',59),('retention_seconds',86401),('retention_seconds',True),('retention_seconds',60.0),
    ('max_log_bytes',262144),('max_log_bytes',266239),('max_log_bytes',134217729),('max_log_bytes',True),
    ('ttl_seconds',59),('ttl_seconds',172801),('ttl_seconds',True),('ttl_seconds',3600.0)])
def test_invalid_yaml_is_rejected_by_both_roles(config,field,value):
    import json
    write_policy(config,f'  {field}: {json.dumps(value)}\n')
    for load in LOADERS:
        with pytest.raises(ValueError):
            load()


@pytest.mark.parametrize('field,value',[('retention_seconds','59'),('max_log_bytes','0'),('max_log_bytes','266239'),
    ('ttl_seconds','172801'),('ttl_seconds','true'),('retention_seconds','60.0'),('max_log_bytes','NaN')])
def test_invalid_environment_is_rejected_by_both_roles(config,monkeypatch,field,value):
    monkeypatch.setenv('WORKMATE_REALTIME_'+field.upper(),value)
    for load in LOADERS:
        with pytest.raises(ValueError):
            load()


@pytest.mark.parametrize('source',['yaml','environment'])
def test_ttl_must_cover_retention(config,monkeypatch,source):
    if source=='yaml':
        write_policy(config,'  retention_seconds: 3600\n  ttl_seconds: 3599\n')
    else:
        monkeypatch.setenv('WORKMATE_REALTIME_TTL_SECONDS','3599')
    for load in LOADERS:
        with pytest.raises(ValueError,match='retention'):
            load()


@pytest.mark.parametrize('retention,size,ttl',[(60,266240,60),(86400,134217728,172800)])
def test_inclusive_boundaries(config,retention,size,ttl):
    write_policy(config,f'  retention_seconds: {retention}\n  max_log_bytes: {size}\n  ttl_seconds: {ttl}\n')
    for load in LOADERS:
        assert load().realtime.ttl_seconds==ttl


def test_unknown_policy_keys_fail(config):
    write_policy(config,'  maxlen: 123\n')
    for load in LOADERS:
        with pytest.raises(ValueError):
            load()


def test_helper_accepts_lowercase_environment_and_ignores_empty():
    assert realtime_overrides({'workmate_realtime_retention_seconds':'120','WORKMATE_REALTIME_TTL_SECONDS':''})=={'retention_seconds':120}


@pytest.mark.parametrize('field', COMPONENT_FIELDS)
@pytest.mark.parametrize('source', ['yaml', 'environment'])
def test_component_policy_cross_role_overrides(config, monkeypatch, field, source):
    value = COMPONENT_FIELDS[field][2]
    write_policy(config, f'  {field}: {DEFAULTS[field]}\n')
    if source == 'yaml':
        write_policy(config, f'  {field}: {value}\n')
    else:
        monkeypatch.setenv('WORKMATE_REALTIME_' + field.upper(), str(value))
    policies = [load().realtime for load in LOADERS]
    assert policies[0] == policies[1]
    assert getattr(policies[0], field) == value


@pytest.mark.parametrize('field', COMPONENT_FIELDS)
@pytest.mark.parametrize('boundary', [0, 1])
def test_component_policy_inclusive_bounds(config, field, boundary):
    value = COMPONENT_FIELDS[field][boundary]
    extra = '  operation_timeout_seconds: 0.1\n' if field == 'outage_grace_seconds' else ''
    write_policy(config, f'  {field}: {value}\n' + extra)
    for load in LOADERS:
        assert getattr(load().realtime, field) == value


@pytest.mark.parametrize('field', COMPONENT_FIELDS)
@pytest.mark.parametrize('invalid', ['below', 'above', 'boolean', 'nan', 'inf', 'negative_inf'])
@pytest.mark.parametrize('source', ['yaml', 'environment'])
def test_component_policy_invalid_values_fail_in_both_roles(config, monkeypatch, field, invalid, source):
    lower, upper, _ = COMPONENT_FIELDS[field]
    value = {'below': str(lower - 1), 'above': str(upper + 1), 'boolean': 'true',
             'nan': '.nan' if source == 'yaml' else 'NaN',
             'inf': '.inf' if source == 'yaml' else 'inf',
             'negative_inf': '-.inf' if source == 'yaml' else '-inf'}[invalid]
    if source == 'yaml':
        write_policy(config, f'  {field}: {value}\n')
    else:
        monkeypatch.setenv('WORKMATE_REALTIME_' + field.upper(), value)
    for load in LOADERS:
        with pytest.raises(ValueError):
            load()


@pytest.mark.parametrize('field', ['snapshot_max_bytes', 'max_buffer_bytes', 'max_buffer_events',
    'reader_page_events', 'client_buffer_events', 'client_buffer_bytes', 'reader_block_ms',
    'reset_max_rows', 'reset_max_bytes', 'max_gateway_readers', 'max_gateway_subscriptions'])
@pytest.mark.parametrize('source', ['yaml', 'environment'])
def test_component_integer_budgets_reject_float_values(config, monkeypatch, field, source):
    value = str(float(COMPONENT_FIELDS[field][2]))
    if source == 'yaml':
        write_policy(config, f'  {field}: {value}\n')
    else:
        monkeypatch.setenv('WORKMATE_REALTIME_' + field.upper(), value)
    for load in LOADERS:
        with pytest.raises(ValueError):
            load()


@pytest.mark.parametrize('source', ['yaml', 'environment'])
def test_outage_grace_covers_operation_timeout(config, monkeypatch, source):
    if source == 'yaml':
        write_policy(config, '  operation_timeout_seconds: 3.5\n  outage_grace_seconds: 3\n')
    else:
        monkeypatch.setenv('WORKMATE_REALTIME_OPERATION_TIMEOUT_SECONDS', '3.5')
        monkeypatch.setenv('WORKMATE_REALTIME_OUTAGE_GRACE_SECONDS', '3')
    for load in LOADERS:
        with pytest.raises(ValueError, match='outage_grace_seconds'):
            load()


def test_writer_helper_handles_lowercase_fractional_values_and_empty():
    assert realtime_overrides({'workmate_realtime_snapshot_interval_seconds': '1.25',
        'WORKMATE_REALTIME_OPERATION_TIMEOUT_SECONDS': ''}) == {'snapshot_interval_seconds': 1.25}
