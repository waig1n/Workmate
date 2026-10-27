"""Reject accidental paid credentials, public egress, and unbounded test workloads."""
import pytest
from scripts.reliability.load_support.isolation import verify_isolation


def blocked(*args, **kwargs):
    raise OSError('network unreachable')


def read_limit(path):
    return {'memory.max':'3221225472','pids.max':'512','cpu.max':'200000 100000'}[path.rsplit('/',1)[-1]]


def test_bounded_zero_cost_environment():
    report=verify_isolation({'WORKMATE_LOAD_MODEL_KEY':'local-fixture-only'},blocked,read_limit)
    assert report['passed'] and report['public_tcp_blocked']


@pytest.mark.parametrize('env',[{}, {'WORKMATE_LOAD_MODEL_KEY':'real-key'},
    {'WORKMATE_LOAD_MODEL_KEY':'local-fixture-only','OPENAI_API_KEY':'unexpected'},
    {'WORKMATE_LOAD_MODEL_KEY':'local-fixture-only','AWS_SECRET_ACCESS_KEY':'unexpected'}])
def test_rejects_missing_fixture_or_external_credentials(env):
    with pytest.raises(RuntimeError):verify_isolation(env,blocked,read_limit)


def test_public_egress_is_failure():
    class Socket:
        def close(self):pass
    with pytest.raises(RuntimeError,match='egress'):
        verify_isolation({'WORKMATE_LOAD_MODEL_KEY':'local-fixture-only'},lambda *a,**k:Socket(),read_limit)


@pytest.mark.parametrize('key',['memory.max','pids.max','cpu.max'])
def test_unbounded_cgroup_is_failure(key):
    def read(path):return 'max 100000' if path.endswith(key) else read_limit(path)
    with pytest.raises(RuntimeError):verify_isolation({'WORKMATE_LOAD_MODEL_KEY':'local-fixture-only'},blocked,read)


def test_missing_cgroup_is_failure():
    def read(path):raise FileNotFoundError(path)
    with pytest.raises(FileNotFoundError):verify_isolation({'WORKMATE_LOAD_MODEL_KEY':'local-fixture-only'},blocked,read)


def test_load_plan_has_enforced_resource_limits():
    from scripts.reliability.load import parse_args,plan
    value=plan(parse_args(['--gateway-image','g','--runtime-image','r']))
    assert value['resource_limits']['workload']['memory_mib']==3072
    assert value['resource_limits']['workload']['cpus']==2


def test_v1_limits_are_checked_without_weakening_bounds():
    values={'memory.limit_in_bytes':'3221225472','pids.max':'512','cpu.cfs_quota_us':'200000','cpu.cfs_period_us':'100000'}
    def read(path):
        if path.count('/')==4:raise FileNotFoundError(path)
        return values[path.rsplit('/',1)[-1]]
    assert verify_isolation({'WORKMATE_LOAD_MODEL_KEY':'local-fixture-only'},blocked,read)['cgroup_version']==1
    values['memory.limit_in_bytes']=str(2**63-4096)
    with pytest.raises(RuntimeError,match='Unbounded'):verify_isolation({'WORKMATE_LOAD_MODEL_KEY':'local-fixture-only'},blocked,read)
