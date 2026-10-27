"""Baseline reports must not turn short, missing or failed samples into a pass."""
import pytest
from scripts.reliability.baseline import disk_budget, summarize


def sample(t=0, **changes):
    return {'elapsed_seconds':t,'hosts':{'local':{'available_bytes':8*2**30,
        'disk_free_bytes':37*2**30,'health_status':200,'load1':0.2,**changes}}}


def test_disk_budget_uses_increment_and_reserve_not_fixed_sixty():
    assert disk_budget(37*2**30, 2*2**30, 5*2**30, 20*2**30)['passed']
    assert not disk_budget(26*2**30, 2*2**30, 5*2**30, 20*2**30)['passed']


@pytest.mark.parametrize('values', [(-1,0,0,0),(1,-1,0,0),(1,0,-1,0),(1,0,0,-1)])
def test_disk_budget_rejects_invalid(values):
    with pytest.raises(ValueError):disk_budget(*values)


def test_complete_window():
    rows=[sample(t) for t in range(0,3601,60)]
    result=summarize(rows,3600,60,('local',))
    assert result['passed'] and result['samples']==61


@pytest.mark.parametrize('rows', [[],[sample()], [sample(0),sample(3600)],
    [sample(0),sample(60,health_status=503)],
    [sample(0),{'elapsed_seconds':60,'hosts':{}}]])
def test_incomplete_or_unhealthy_window(rows):
    assert not summarize(rows,3600,60,('local',))['passed']


def test_error_sample_is_not_silently_dropped():
    rows=[sample(),{'elapsed_seconds':60,'hosts':{'local':{'error':'TimeoutExpired'}}}]
    result=summarize(rows,60,60,('local',))
    assert not result['passed'] and result['errors']


def test_short_valid_window_is_explicit():
    result=summarize([sample(0),sample(1)],1,1,('local',))
    assert result['passed'] and result['requested_seconds']==1


def test_low_memory_disk_and_gap_fail():
    result=summarize([sample(0),sample(100,available_bytes=1,disk_free_bytes=1)],100,1,('local',))
    assert {'sampling_gap','local:low_memory','local:low_disk'} <= set(result['errors'])


def test_cli_preview_no_probes(tmp_path, monkeypatch, capsys):
    from scripts.reliability import baseline as b
    monkeypatch.setattr(b,'probe',lambda *_:pytest.fail('preview performed I/O'))
    assert b.main(['--remote','operator@example.test','--output',str(tmp_path/'unused')])==0
    assert not (tmp_path/'unused').exists()
    assert '"run": false' in capsys.readouterr().out


@pytest.mark.parametrize('args',[['--remote','-oProxyCommand=bad'],['--duration-seconds','0'],['--interval-seconds','61']])
def test_invalid_cli(tmp_path,args):
    from scripts.reliability.baseline import main
    with pytest.raises(SystemExit):main(['--output',str(tmp_path),*args])


def test_bounded_sampling_and_summary(tmp_path,monkeypatch):
    import json
    from scripts.reliability import baseline as b
    clock=[0.0]
    monkeypatch.setattr(b.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(b.time,'sleep',lambda n:clock.__setitem__(0,clock[0]+n))
    monkeypatch.setattr(b,'probe',lambda *_:sample()['hosts']['local'])
    output=tmp_path/'run'
    assert b.main(['--remote','operator@example.test','--output',str(output),'--duration-seconds','2','--interval-seconds','1','--run'])==0
    assert json.loads((output/'result.json').read_text())['samples']==3
    with pytest.raises(FileExistsError):b.main(['--remote','operator@example.test','--output',str(output),'--run'])


def test_probe_filters_errors_and_uses_stdin(monkeypatch):
    from types import SimpleNamespace
    from scripts.reliability import baseline as b
    calls=[]
    def success(command,**kw):
        calls.append((command,kw));return SimpleNamespace(stdout='{"health_status":200}')
    monkeypatch.setattr(b.subprocess,'run',success)
    assert b.probe()['health_status']==200
    assert b.probe('operator@example.test')['health_status']==200
    assert calls[-1][0][-1]=='python3 -' and calls[-1][1]['input']==b.PROBE
    def failure(*a,**k):raise OSError('secret')
    monkeypatch.setattr(b.subprocess,'run',failure)
    assert b.probe()=={'error':'OSError'}
