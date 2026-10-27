import pytest
from scripts.reliability.load_support.safety import safety_errors, process_tree


def sample():
    return {'available_bytes':8*2**30,'disk_free_bytes':50*2**30,'disk_total_bytes':300*2**30,
            'oom_kill':0,'evidence_bytes':1024}


def test_safe_sample():
    assert safety_errors(sample(),0)==[]


@pytest.mark.parametrize('key,value',[('available_bytes',0),('disk_free_bytes',25*2**30),
    ('oom_kill',1),('evidence_bytes',4*2**30)])
def test_resource_breach_stops_admission(key,value):
    row=sample();row[key]=value
    assert safety_errors(row,0)


def test_missing_metrics_are_failure():
    with pytest.raises(KeyError):safety_errors({},0)


def test_process_tree_includes_children_and_fd(tmp_path):
    for pid,ppid in [(1,0),(2,1),(3,2),(4,0)]:
        p=tmp_path/str(pid);p.mkdir();(p/'fd').mkdir();(p/'fd'/'0').write_text('')
        fields=['S',str(ppid)]+['0']*22
        fields[11]='100';fields[12]='50';fields[21]='2'
        (p/'stat').write_text(f'{pid} (fixture with spaces) '+' '.join(fields))
    rows=process_tree(1,tmp_path)
    assert {r['pid'] for r in rows}=={1,2,3}
    assert all(r['fds']==1 and r['rss_bytes']>0 for r in rows)
