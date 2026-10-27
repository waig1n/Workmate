import pytest
from scripts.reliability.remote_fixture import validate_owner, limits, environment, tunnel, verify_labels


def test_owner_paths_strict():
    assert validate_owner('capacity-0123456789ab')=='/tmp/capacity-0123456789ab'
    for value in ['business','capacity-../../etc','capacity-0123456789ab/','capacity-']:
        with pytest.raises(ValueError):validate_owner(value)


def test_layout_resource_budget_and_synthetic_env():
    assert limits('app')['memory_mib']==4096
    assert sum(limits(x)['cpus'] for x in ['app','pg','redis','s3'])==3.75
    env=environment('capacity-0123456789ab')
    assert env['PYTHONPATH'].startswith('/capacity:/support:')
    assert 'WORKMATE_LOAD_NAMESPACE' in env and '.env' not in str(env)
    assert 'capacity-0123456789ab-pg' in env['DATABASE_URL']


def test_cleanup_rejects_foreign_labels():
    verify_labels({'workmate.capacity.owner':'capacity-0123456789ab'},'capacity-0123456789ab')
    with pytest.raises(ValueError):verify_labels({'workmate.capacity.owner':'business'},'capacity-0123456789ab')


def test_tunnel_only_loopback_source_and_private_destination():
    cmd=tunnel('172.18.0.5',18080)
    assert '127.0.0.1:18080:172.18.0.5:8080' in cmd
    with pytest.raises(ValueError):tunnel('8.8.8.8',18080)


def test_snapshot_excludes_dotfiles_keys_and_symlinks(tmp_path,monkeypatch):
    from scripts.reliability import remote_fixture as mod
    root=tmp_path/'repo';root.mkdir();(root/'backend').mkdir();(root/'backend/alembic.ini').write_text('safe')
    source=root/'config';source.mkdir();(source/'workmate.yaml').write_text('safe')
    (source/'.env').write_text('secret');(source/'private.key').write_text('secret')
    (source/'link.yaml').symlink_to(source/'.env')
    monkeypatch.setattr(mod,'ROOT',root);monkeypatch.setattr(mod,'ALLOW',('config',))
    dest=tmp_path/'copy';(dest/'backend').mkdir(parents=True)
    assert set(mod.source_snapshot(dest))=={'config/workmate.yaml','backend/alembic.ini'}


def test_cli_dry_run_never_calls_external_process(monkeypatch,capsys):
    from scripts.reliability import remote_fixture as mod
    monkeypatch.setattr(mod,'run_cmd',lambda *a,**k:pytest.fail('External command'))
    mod.main(['provision'])
    assert '"run": false' in capsys.readouterr().out


def test_collect_captures_stderr_and_archive(tmp_path):
    from scripts.reliability import remote_fixture as mod
    from types import SimpleNamespace
    r=mod.Remote({'owner':'capacity-0123456789ab','remote_created':True},tmp_path/'state.json')
    r.docker=lambda *a:'owned-container'
    r.verify=lambda *a:None
    r.ssh=lambda args,**kw:SimpleNamespace(stdout=b'archive' if 'tar' in args else b'out',stderr=b'err')
    r.status=lambda **kw:{'ok':True}
    assert r.collect()=={'ok':True}
    assert (tmp_path/'collected/owned-container.log').read_bytes()==b'outerr'
    assert (tmp_path/'collected/evidence.tar.gz').read_bytes()==b'archive'


def test_cleanup_refuses_foreign_resource_before_mutation(tmp_path):
    from scripts.reliability import remote_fixture as mod
    r=mod.Remote({'owner':'capacity-0123456789ab'},tmp_path/'state.json');calls=[]
    def docker(*args):
        calls.append(args)
        if args[0]=='ps':return 'foreign'
        if args[0]=='inspect':return '[{"Config":{"Labels":{"workmate.capacity.owner":"business"}}}]'
        pytest.fail('Mutation was attempted')
    r.docker=docker
    with pytest.raises(ValueError):r.cleanup()
    assert all(c[0] in ('ps','inspect') for c in calls)


def test_cleanup_checks_label_and_preserves_business(tmp_path):
    from scripts.reliability import remote_fixture as mod
    import json
    state={'owner':'capacity-0123456789ab','business_before':{'business':{'started':'before','restarts':0}}}
    r=mod.Remote(state,tmp_path/'state.json');calls=[];removed=set()
    def docker(*args):
        calls.append(args)
        if args[0]=='ps':return '' if 'owned' in removed else 'owned'
        if args[:2]==('network','ls'):return '' if 'net' in removed else 'net'
        if args[0]=='volume':return ''
        if args[0]=='inspect' and args[1]=='--size':return json.dumps([{'Config':{'Labels':{mod.LABEL:state['owner']}},'Mounts':[]}])
        if args[0]=='inspect' and 'State' in args[-1]:return json.dumps({'Running':False,'ExitCode':0,'OOMKilled':False})
        if args[0]=='inspect':
            return json.dumps({mod.LABEL:state['owner']}) if 'Labels' in args[-1] else '/capacity-0123456789ab-app'
        if args[:2]==('network','inspect'):return json.dumps({mod.LABEL:state['owner']})
        if args[0]=='rm':removed.add('owned')
        if args[:2]==('network','rm'):removed.add('net')
        return ''
    r.docker=docker;r.collect=lambda **kw:{};r.inventory=lambda:state['business_before']
    result=r.cleanup()
    assert result['passed'] and result['business_preserved']
    assert ('stop','--time','65','owned') in calls
    assert ('rm','-f','-v','owned') in calls
    assert json.loads((tmp_path/'cleanup.json').read_text())['remaining_containers']==[]


def test_provision_failure_is_saved_without_printing_stderr(tmp_path,monkeypatch,capsys):
    from scripts.reliability import remote_fixture as mod
    import subprocess,json
    owner='capacity-0123456789ab'
    def fail(args):
        (args.output/args.owner).mkdir()
        raise subprocess.CalledProcessError(1,['synthetic-command'],output=b'out',stderr=b'private-diagnostic')
    monkeypatch.setattr(mod,'provision',fail)
    with pytest.raises(SystemExit) as exc:
        mod.main(['provision','--runtime-image','workmate-runtime:test','--gateway-image','workmate-gateway:test','--owner',owner,'--output',str(tmp_path),'--run'])
    assert exc.value.code==1
    assert 'private-diagnostic' not in capsys.readouterr().out
    assert json.loads((tmp_path/owner/'provision-error.json').read_text())['stderr']=='private-diagnostic'


def test_runtime_disk_counts_logs_writable_and_anonymous(tmp_path):
    from scripts.reliability import remote_fixture as mod
    r=mod.Remote({'owner':'capacity-0123456789ab'},tmp_path/'state.json')
    r.ssh=lambda args,**kw:__import__('types').SimpleNamespace(stdout=b'100 path\n')
    info={'Id':'abc','LogPath':'/var/lib/docker/containers/abc/abc-json.log','SizeRw':31,
          'Mounts':[{'Type':'volume','Name':'v1','Source':'/var/lib/docker/volumes/v1/_data'}]}
    result=r.runtime_disk({'abc':info})
    assert result['extra_bytes']==231
    assert result['anonymous_volumes']=={'v1':{'bytes':100,'source':'/var/lib/docker/volumes/v1/_data'}}


def test_shutdown_receipt_requires_matching_pid_and_clean_exit():
    from scripts.reliability.remote_fixture import shutdown_errors
    good={'ExitCode':0,'OOMKilled':False,'Running':False}
    assert shutdown_errors(good,{'pid':7},{'pid':7,'passed':True},True)==[]
    assert shutdown_errors(good,{'pid':7},{'pid':8,'passed':True},True)
    assert shutdown_errors({**good,'OOMKilled':True},{'pid':7},{'pid':7,'passed':True},True)
    assert shutdown_errors({**good,'ExitCode':137},{'pid':7},{'pid':7,'passed':True},True)


def test_status_stopped_or_missing_container_fails_but_collection_allows_stop(tmp_path):
    from scripts.reliability import remote_fixture as mod
    owner='capacity-0123456789ab'
    r=mod.Remote({'owner':owner,'containers':[owner+'-app',owner+'-pg']},tmp_path/'state.json')
    r.inspect_owned=lambda:{'app':{'Name':'/'+owner+'-app','State':{'Running':False},'HostConfig':{'Memory':4096*2**20}}}
    r.sample=lambda:{'data_bytes':1,'available_bytes':3*2**30,'disk_free_bytes':40*2**30,'disk_total_bytes':100*2**30}
    r.runtime_disk=lambda infos:{'extra_bytes':2}
    report=r.status()
    assert not report['passed'] and len(report['stop_reasons'])==2
    assert report['resources']['data_bytes']==3
    assert report['host_configs']['app']['Memory']==4096*2**20
    assert r.status(allow_stopped=True)['passed']


def test_status_budget_cli_returns_nonzero(tmp_path,monkeypatch):
    from scripts.reliability import remote_fixture as mod
    import json
    path=tmp_path/'state.json';path.write_text(json.dumps({'owner':'capacity-0123456789ab','host':'operator@example.test'}))
    monkeypatch.setattr(mod.Remote,'status',lambda self:{'passed':False,'stop_reasons':['4GiB data budget']})
    with pytest.raises(SystemExit) as exc:mod.main(['status','--state',str(path),'--run'])
    assert exc.value.code==1


def test_cleanup_anonymous_volume_residue_fails_and_keeps_remote_evidence(tmp_path):
    from scripts.reliability import remote_fixture as mod
    owner='capacity-0123456789ab';r=mod.Remote({'owner':owner,'remote_created':True},tmp_path/'state.json')
    r.inspect_owned=lambda:{'owned':{'Mounts':[{'Type':'volume','Name':'exact-anonymous-id','Source':'/owned-volume'}]}}
    r.verify=lambda *a,**kw:None
    r.collect=lambda **kw:{};r.inventory=lambda:{}
    calls=[];removed=False
    def docker(*args):
        nonlocal removed
        calls.append(args)
        if args[0]=='ps':return '' if removed else 'owned'
        if args[0]=='inspect':return '/dependency'
        if args[0]=='rm':removed=True;return ''
        if args[0]=='volume':return 'exact-anonymous-id unrelated-volume'
        return ''
    r.docker=docker
    r.ssh=lambda *a,**kw:pytest.fail('Evidence must not be deleted')
    result=r.cleanup()
    assert result['remaining_volumes']==['exact-anonymous-id']
    assert not result['passed']
    assert result['anonymous_volumes_before']['exact-anonymous-id']['container_id']=='owned'


def test_knowledge_configuration_is_complete_and_isolated():
    env=environment('capacity-0123456789ab')
    assert env['KNOWLEDGE_QDRANT_URL']=='http://127.0.0.1:9'
    assert env['KNOWLEDGE_QDRANT_API_KEY']=='fixture'
    assert env['KNOWLEDGE_S3_ENDPOINT']=='http://capacity-0123456789ab-s3:9000'
    assert env['KNOWLEDGE_S3_BUCKET']=='capacity-knowledge'
    assert env['KNOWLEDGE_S3_ACCESS_KEY']==env['ARTIFACT_S3_ACCESS_KEY']
    assert env['KNOWLEDGE_S3_SECRET_KEY']==env['ARTIFACT_S3_SECRET_KEY']


def test_inventory_one_remote_call_returns_only_business_facts(tmp_path):
    from scripts.reliability import remote_fixture as mod
    from types import SimpleNamespace
    import json
    r=mod.Remote({'owner':'capacity-0123456789ab'},tmp_path/'state.json');calls=[]
    expected={'business':{'started':'2026-10-15T00:00:00Z','restarts':2}}
    def ssh(args,**kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps(expected).encode())
    r.ssh=ssh
    assert r.inventory()==expected
    assert len(calls)==1
    assert calls[0][:2]==['python3','-c']
    assert 'workmate.capacity.owner' in calls[0][2]


def test_inventory_remote_script_filters_owned_and_never_requests_env(tmp_path,monkeypatch):
    from scripts.reliability import remote_fixture as mod
    from types import SimpleNamespace
    import subprocess,json,io,contextlib
    r=mod.Remote({'owner':'capacity-0123456789ab'},tmp_path/'state.json');commands=[]
    def output(args,**kwargs):
        commands.append(args)
        if 'ps' in args:return 'business\nfixture\n'
        return '\n'.join(json.dumps(x) for x in [
            {'id':'business','owner':'','started':'before','restarts':3},
            {'id':'fixture','owner':'capacity-abcdef012345','started':'now','restarts':0}])
    monkeypatch.setattr(subprocess,'check_output',output)
    def ssh(args,**kwargs):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):exec(args[2],{})
        return SimpleNamespace(stdout=stream.getvalue().encode())
    r.ssh=ssh
    assert r.inventory()=={'business':{'started':'before','restarts':3}}
    assert len(commands)==2 and '--format' in commands[1]
    assert 'Env' not in commands[1][commands[1].index('--format')+1]


def test_business_comparison_normalizes_only_unique_legacy_prefix():
    from scripts.reliability.remote_fixture import compare_business
    facts={'started':'before','restarts':0};short='abcdef123456';full=short+'0'*52
    result=compare_business({short:facts},{full:facts})
    assert result['preserved'] and result['normalized_mapping']=={short:full}
    assert not compare_business({short:facts},{full:facts,short+'1'*52:facts})['preserved']
    assert not compare_business({short:facts},{full:{**facts,'restarts':1}})['preserved']
    assert not compare_business({'missing':facts},{full:facts})['preserved']


@pytest.fixture(autouse=True)
def explicit_fixture_remote(monkeypatch):
    monkeypatch.setenv('WORKMATE_TEST_SSH_TARGET', 'operator@example.test')


@pytest.mark.parametrize('environment_target', [None, 'different@example.test'])
def test_provision_uses_explicit_host_for_ssh_and_saved_tunnel(monkeypatch, tmp_path, environment_target):
    from types import SimpleNamespace
    import json
    from scripts.reliability import remote_fixture as mod

    if environment_target is None:
        monkeypatch.delenv('WORKMATE_TEST_SSH_TARGET', raising=False)
    else:
        monkeypatch.setenv('WORKMATE_TEST_SSH_TARGET', environment_target)
    owner = 'capacity-0123456789ab'
    calls = []
    def run(parts, **kwargs):
        calls.append(parts)
        if '{{json .Config.Labels}}' in parts:
            return SimpleNamespace(stdout=json.dumps({mod.LABEL: owner}).encode())
        return SimpleNamespace(stdout=b'sha256:resolved-gateway')
    def docker(self, *parts):
        if '{{json .Config.Labels}}' in parts:
            return json.dumps({mod.LABEL: owner})
        if parts[:2] == ('image', 'inspect'):
            return 'sha256:resolved-remote-image'
        if any('IPAddress' in str(part) for part in parts):
            return '172.18.0.5'
        return ''
    monkeypatch.setattr(mod, 'run_cmd', run)
    monkeypatch.setattr(mod.Remote, 'docker', docker)
    monkeypatch.setattr(mod.Remote, 'inventory', lambda self: {})
    monkeypatch.setattr(mod.Remote, 'sample', lambda self: dict(available_bytes=100*2**30, disk_free_bytes=100*2**30, disk_total_bytes=200*2**30))
    monkeypatch.setattr(mod.shutil, 'disk_usage', lambda _: SimpleNamespace(free=100*2**30, total=200*2**30))
    monkeypatch.setattr(mod, 'source_snapshot', lambda _: {})
    args = SimpleNamespace(owner=owner, output=tmp_path, remote='selected@example.test',
        runtime_image='workmate-runtime:test', gateway_image='workmate-gateway:test',
        postgres_image='postgres:18', redis_image='redis:7', s3_image='rustfs:test',
        workers=1, capacity=2, local_port=18080)
    state = mod.provision(args)
    assert state['host'] == 'selected@example.test'
    assert state['tunnel_argv'][-1] == state['host']
    assert all(parts[5] == state['host'] for parts in calls if parts[0] == 'ssh')
    assert state['gateway_image'] == 'sha256:resolved-gateway'
    creates = [parts for parts in calls if parts[:2] == ['docker', 'create']]
    assert creates and creates[0][-1] == state['gateway_image']
