import pytest
import json
from scripts.reliability.resource_monitor import violations,join_effects


def healthy():
    return {'hosts':{'remote':{'available_bytes':8*2**30,'disk_free_bytes':40*2**30,
        'disk_total_bytes':320*2**30,'health_status':200,'owned_data_bytes':0,'containers':[]}}}


def test_disk_floor_uses_ten_percent_when_larger():
    sample=healthy();assert not violations(sample)
    sample['hosts']['remote']['disk_free_bytes']=30*2**30
    assert violations(sample)==['remote:disk_floor']


def test_full_owned_data_and_oom_fail_closed():
    sample=healthy();sample['hosts']['remote'].update(owned_data_bytes=4*2**30+1,containers=[{'oom_killed':True}])
    assert violations(sample)==['remote:data_cap','remote:container_oom']


def test_duplicate_tool_attempt_cannot_pass_independent_join(tmp_path):
    report={'runs':[{'tasks':[{'key':'a','id':'task','workload':'file'}]}]}
    trace=tmp_path/'write-effects-0.jsonl'
    assert not join_effects(report,tmp_path)['passed']
    line=json.dumps({'file_path':'/workspace/output/a.txt'})+'\n'
    trace.write_text(line);assert join_effects(report,tmp_path)['passed']
    trace.write_text(line*2);assert not join_effects(report,tmp_path)['passed']


def test_nonrunning_or_nonzero_container_fails_closed():
    sample=healthy()
    sample['hosts']['remote']['containers']=[{'status':'exited','exit_code':1}]
    assert violations(sample)==['remote:container_not_running','remote:container_exit']


def test_usage_counts_writable_rotated_logs_and_volumes_once():
    from scripts.reliability.resource_monitor import owned_usage
    rows=[{'SizeRw':10,'LogPath':'/docker/logs/a.json','Mounts':[
        {'Type':'bind','Source':'/tmp/capacity/data/pg'},
        {'Type':'volume','Source':'/docker/volumes/one'},
        {'Type':'volume','Source':'/tmp/capacity/data/already'}]},
        {'SizeRw':20,'LogPath':'/docker/logs/b.json','Mounts':[{'Type':'volume','Source':'/docker/volumes/one'}]}]
    calls=[]
    def measure(path):
        calls.append(path)
        return {'/tmp/capacity/data':80,'/tmp/capacity/evidence':10,'/tmp/capacity/shared':10,'/docker/logs':200,'/docker/volumes/one':300}[path]
    result=owned_usage(rows,'/tmp/capacity',measure)
    assert result['total_bytes']==630
    assert len(calls)==5
    assert not any(path.endswith(('/source','/extra-gateway')) for path in calls)


def test_missing_size_or_container_inventory_cannot_pass():
    import pytest
    from scripts.reliability.resource_monitor import owned_usage
    with pytest.raises(ValueError,match='writable-layer'):
        owned_usage([{}],'/tmp/owner',lambda path:0)
    sample=healthy();sample['hosts']['remote']['owned_containers_expected']=True
    assert violations(sample)==['remote:containers_missing']


def test_remote_probe_embeds_valid_measurement_code(monkeypatch):
    from types import SimpleNamespace
    from scripts.reliability import resource_monitor
    def run(command,**kwargs):
        source=kwargs['input'];compile(source,'remote-probe','exec')
        assert "['inspect','--size',*ids]" in source
        assert "'exit_code':r['State']['ExitCode']" in source
        return SimpleNamespace(stdout='{}\n{"owned_data_bytes":123}\n')
    monkeypatch.setattr(resource_monitor.subprocess,'run',run)
    assert resource_monitor.probe('capacity-123456abcdef',True)['owned_data_bytes']==123


def test_failed_remote_usage_preserves_host_sample_and_redacts_diagnostic(monkeypatch):
    import subprocess
    from scripts.reliability import resource_monitor
    host=healthy()['hosts']['remote']
    def run(*args,**kwargs):
        raise subprocess.CalledProcessError(1,['ssh'],output=json.dumps(host)+'\n',
            stderr='du: Permission denied password=secret-value https://user:secret@host/path')
    monkeypatch.setattr(resource_monitor.subprocess,'run',run)
    row=resource_monitor.probe('capacity-123456abcdef',True)
    assert row['available_bytes']==host['available_bytes']
    error=row['sample_errors'][0]
    assert error['returncode']==1 and error['stage']=='remote_transport'
    assert 'Permission denied' in error['stderr_summary']
    assert 'secret-value' not in error['stderr_summary'] and 'user:secret' not in error['stderr_summary']
    row['available_bytes']=1
    assert violations({'hosts':{'remote':row}})==['remote:sample_error','remote:memory_floor']


def test_remote_structured_error_retains_command_stage(monkeypatch):
    from types import SimpleNamespace
    from scripts.reliability import resource_monitor
    host=healthy()['hosts']['remote']
    failure={'error':'CalledProcessError','sample_errors':[{'stage':'owned_usage','command_category':'owned_du','error_type':'CalledProcessError','returncode':1,'stderr_summary':'du: file vanished'}]}
    monkeypatch.setattr(resource_monitor.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=json.dumps(host)+'\n'+json.dumps(failure)+'\n'))
    row=resource_monitor.probe('capacity-123456abcdef',True)
    assert row['sample_errors'][0]['command_category']=='owned_du'
    assert row['health_status']==200
    assert violations({'hosts':{'remote':row}})==['remote:sample_error']


def test_observer_failure_exits_nonzero_and_writes_complete_stop(tmp_path,monkeypatch):
    from scripts.reliability import resource_monitor
    def probe(owner,remote):
        row=healthy()['hosts']['remote'].copy()
        if remote:row.update(error='RuntimeError',sample_errors=[{'stage':'owned_usage','error_type':'RuntimeError'}])
        return row
    monkeypatch.setattr(resource_monitor,'probe',probe)
    assert resource_monitor.main(['--owner','capacity-123456abcdef','--output',str(tmp_path),'--duration','1'])==1
    stop=json.loads((tmp_path/'STOP.json').read_text())
    assert stop['reasons']==['remote:sample_error']
    assert stop['sample']['hosts']['remote']['available_bytes']>0
    assert json.loads((tmp_path/'monitor-summary.json').read_text())['passed'] is False
    assert not list(tmp_path.glob('*.tmp'))


def test_apparent_tree_deduplicates_hardlinks_and_does_not_follow_symlinks(tmp_path):
    import os
    from scripts.reliability.resource_monitor import apparent_tree_usage
    root=tmp_path/'root';root.mkdir();(root/'data').write_bytes(b'abc')
    os.link(root/'data',root/'alias')
    outside=tmp_path/'outside';outside.write_bytes(b'x'*1000)
    (root/'link').symlink_to(outside)
    result=apparent_tree_usage(str(root))
    assert result['total_bytes']==root.stat().st_size+3+(root/'link').lstat().st_size
    assert result['disappeared_entries']==0


def test_tree_only_descendant_enoent_is_counted(tmp_path,monkeypatch):
    import errno,os
    from scripts.reliability.resource_monitor import apparent_tree_usage
    (tmp_path/'vanished').write_text('a')
    original=os.stat
    def stat(path,*args,**kwargs):
        if path=='vanished' and kwargs.get('dir_fd') is not None:raise FileNotFoundError(errno.ENOENT,'gone',path)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(os,'stat',stat)
    result=apparent_tree_usage(str(tmp_path))
    assert result['disappeared_entries']==1


def test_tree_missing_root_permission_and_io_errors_fail_closed(tmp_path,monkeypatch):
    import errno,os,pytest
    from scripts.reliability.resource_monitor import apparent_tree_usage
    with pytest.raises(FileNotFoundError):apparent_tree_usage(str(tmp_path/'missing'))
    assert apparent_tree_usage(str(tmp_path/'missing'),optional=True)['present'] is False
    (tmp_path/'child').write_text('a');original=os.stat
    for number in (errno.EACCES,errno.EIO):
        def stat(path,*args,**kwargs):
            if path=='child' and kwargs.get('dir_fd') is not None:raise OSError(number,'denied',path)
            return original(path,*args,**kwargs)
        monkeypatch.setattr(os,'stat',stat)
        with pytest.raises(OSError) as caught:apparent_tree_usage(str(tmp_path))
        assert caught.value.errno==number


def test_probe_timeout_retains_partial_host_output(monkeypatch):
    import subprocess
    from scripts.reliability import resource_monitor
    host=healthy()['hosts']['remote']
    def run(*args,**kwargs):raise subprocess.TimeoutExpired(['ssh'],25,output=(json.dumps(host)+'\n').encode(),stderr=b'bounded timeout')
    monkeypatch.setattr(resource_monitor.subprocess,'run',run)
    row=resource_monitor.probe('capacity-123456abcdef',True)
    assert row['health_status']==200
    assert row['sample_errors'][0]['error_type']=='TimeoutExpired'


def test_unexpected_probe_failure_still_creates_stop(tmp_path,monkeypatch):
    from scripts.reliability import resource_monitor
    def probe(*args):raise RuntimeError('fixture')
    monkeypatch.setattr(resource_monitor,'probe',probe)
    assert resource_monitor.main(['--owner','capacity-123456abcdef','--output',str(tmp_path),'--duration','1'])==1
    assert json.loads((tmp_path/'STOP.json').read_text())['reasons']==['local:sample_error','remote:sample_error']


def test_long_stderr_keeps_terminal_error_and_redacts_secrets():
    from scripts.reliability.resource_monitor import safe_stderr
    value=safe_stderr('Traceback '+('x'*2000)+'\nPermissionError: EACCES password=hidden-secret')
    assert len(value)<=800 and 'PermissionError: EACCES' in value and 'hidden-secret' not in value


def test_missing_one_expected_role_fails_closed():
    sample=healthy();row=sample['hosts']['remote']
    row['expected_container_names']=['owner-app','owner-pg','owner-redis','owner-s3']
    row['containers']=[{'name':'/owner-'+role,'status':'running','exit_code':0} for role in ('app','pg','redis')]
    assert violations(sample)==['remote:container_inventory_mismatch']


def test_empty_remote_json_rows_do_not_satisfy_required_measurements(monkeypatch):
    from types import SimpleNamespace
    from scripts.reliability import resource_monitor
    host=healthy()['hosts']['remote'];host.pop('owned_data_bytes')
    monkeypatch.setattr(resource_monitor.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=json.dumps(host)+'\n{}\n{}\n{}\n'))
    row=resource_monitor.probe('capacity-123456abcdef',True)
    assert row['error']=='IncompleteSample'
    assert row['sample_errors'][-1]['command_category']=='missing_fields'


@pytest.fixture(autouse=True)
def explicit_fixture_remote(monkeypatch):
    monkeypatch.setenv('WORKMATE_TEST_SSH_TARGET', 'operator@example.test')
