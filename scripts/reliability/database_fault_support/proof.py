"""Read-only Linux process identity evidence, shared with pure driver tests."""
from pathlib import Path


def identity(pid, proc=Path('/proc')):
    try:
        fields=(proc/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
        return {'pid':int(pid),'starttime':int(fields[19]),'pgrp':int(fields[2])}
    except FileNotFoundError:
        return None


def disappearance(before, proc=Path('/proc')):
    current=identity(before['pid'],proc)
    group=[]
    for path in proc.iterdir():
        if path.name.isdigit():
            row=identity(path.name,proc)
            if row and row['pgrp']==before['pgrp']:group.append(row)
    return {'before':before,'current':current,'group_members':group,
        'gone':(current is None or current['starttime']!=before['starttime']) and not group}


ALLOWED_FAILURES = (('TimeoutError','CancelledError'),('ExecutionAborted','TimeoutError'))


def verify_failure(task, worker_failures, worker_log, *, task_id, session_id):
    import json
    error=task['error']
    if isinstance(error,str):error=json.loads(error)
    assert task['status']=='failed' and error['code']=='runtime_execution_quarantined'
    context=error['context']
    assert context['execution']['session_id']==session_id
    pair=(context['error_type'],context['cause_type'])
    assert pair in ALLOWED_FAILURES, pair
    assert worker_failures=={session_id:pair[0]},worker_failures
    lines=[line for line in worker_log.splitlines() if line.startswith('Worker session blocked ')]
    assert len(lines)==1,lines
    tokens=dict(word.split('=',1) for word in lines[0].split() if '=' in word)
    assert tokens['session_id']==session_id and tokens['task_id']==task_id,tokens
    assert (tokens['exception_type'],tokens['cause_type'])==pair,tokens
    return {'error_type':pair[0],'cause_type':pair[1],'task_id':task_id,'session_id':session_id}
