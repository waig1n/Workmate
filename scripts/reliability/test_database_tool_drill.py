"""Fault injection cannot target an unowned PostgreSQL container."""
import json
import pytest
from scripts.reliability import database_tool_drill as drill


class Stop:
    def wait(self, timeout):return False


def test_unowned_pg_is_never_paused(tmp_path,monkeypatch):
    evidence=tmp_path/'reliability-load-012345abcdef'/'evidence';evidence.mkdir(parents=True)
    (evidence/'pg-pause-request.json').write_text('{}')
    monkeypatch.setattr(drill.subprocess,'check_output',lambda *a,**kw:json.dumps({'workmate.validation.owner':'other'}))
    mutations=[]
    monkeypatch.setattr(drill.subprocess,'run',lambda *a,**kw:mutations.append(a))
    errors=[];drill.inject(tmp_path,Stop(),errors)
    assert errors and not mutations
    assert not (evidence/'pg-paused.json').exists()


def test_fault_restores_exact_owned_pg_and_records_receipt(tmp_path,monkeypatch):
    owner='reliability-load-012345abcdef'
    evidence=tmp_path/owner/'evidence';evidence.mkdir(parents=True)
    (evidence/'pg-pause-request.json').write_text('{}')
    monkeypatch.setattr(drill.subprocess,'check_output',lambda *a,**kw:json.dumps({'workmate.validation.owner':owner}))
    mutations=[]
    monkeypatch.setattr(drill.subprocess,'run',lambda command,**kw:mutations.append(command))
    errors=[];drill.inject(tmp_path,Stop(),errors)
    assert errors==[]
    assert mutations==[['docker','pause',owner+'-pg'],['docker','unpause',owner+'-pg']]
    assert (evidence/'pg-paused.json').exists() and (evidence/'pg-restored.json').exists()


def test_process_proof_requires_identity_and_whole_group_gone(tmp_path):
    from scripts.reliability.database_fault_support.proof import identity,disappearance
    def process(pid,starttime,pgrp):
        path=tmp_path/str(pid);path.mkdir(exist_ok=True)
        fields=['S','1',str(pgrp)]+['0']*16+[str(starttime)]
        (path/'stat').write_text(str(pid)+' (fixture with space) '+' '.join(fields))
    process(42,100,42)
    before=identity(42,tmp_path)
    assert before=={'pid':42,'starttime':100,'pgrp':42}
    assert not disappearance(before,tmp_path)['gone']
    # A reaped leader does not prove descendants have left the original group.
    (tmp_path/'42'/'stat').unlink();process(43,110,42)
    assert not disappearance(before,tmp_path)['gone']
    (tmp_path/'43'/'stat').unlink()
    assert disappearance(before,tmp_path)['gone']
    # Reused PID with a different start time and group is not the original child.
    process(42,200,99)
    assert disappearance(before,tmp_path)['gone']


@pytest.mark.parametrize('pair',[('TimeoutError','CancelledError'),('ExecutionAborted','TimeoutError')])
def test_exact_failure_pair_requires_matching_durable_identity_and_log(pair):
    from scripts.reliability.database_fault_support.proof import verify_failure
    task={'status':'failed','error':{'code':'runtime_execution_quarantined',
        'context':{'execution':{'session_id':'s'},'error_type':pair[0],'cause_type':pair[1]}}}
    log=f'Worker session blocked session_id=s task_id=t command_id=c kind=StartTask exception_type={pair[0]} cause_type={pair[1]}'
    assert verify_failure(task,{'s':pair[0]},log,task_id='t',session_id='s')['error_type']==pair[0]
    with pytest.raises(AssertionError):verify_failure(task,{'s':pair[0]},log,task_id='different',session_id='s')
    with pytest.raises(AssertionError):verify_failure(task,{'s':pair[0]},log+'\n'+log,task_id='t',session_id='s')
    task['error']['context']['cause_type']='ValueError'
    with pytest.raises(AssertionError):verify_failure(task,{'s':pair[0]},log,task_id='t',session_id='s')
