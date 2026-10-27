"""Historical facts are reusable only through exact, consecutive recovery edges."""
import pytest
from workmate_contracts import snapshot_digest
from workmate_product.persistence.recovery_provenance import valid_lineage


def edge(source=1):
    snapshot={'state':{'epoch':source}}
    row=dict(snapshot_id=f'snapshot-{source}',task_id='task',session_id='session',storage_node_id='node',
        source_worker_id=f'worker-{source}',source_epoch=source,resource_generation='generation',
        snapshot_digest=snapshot_digest(snapshot),snapshot_payload=snapshot,status='released',
        resume_worker_id=f'worker-{source+1}',resume_epoch=source+1,resume_command_id=f'command-{source}',
        command_status='completed',command_worker=f'worker-{source+1}',command_epoch=source+1,
        command_source_epoch=source,command_task='task',command_session='session',command_kind='ResumeInteraction')
    row['cleanup_receipt']=dict(schema_version=1,task_id='task',session_id='session',node_id='node',
        worker_id=row['source_worker_id'],execution_epoch=source,snapshot_id=row['snapshot_id'],
        snapshot_digest=row['snapshot_digest'],resource_generation='generation',resources_quiescent=True,workspace_closed=True)
    return row


def check(rows,epoch=3,**kwargs):
    return valid_lineage(rows,task_id='task',session_id='session',node_id='node',source_worker='worker-1',
        source_epoch=1,worker_id=f'worker-{epoch}',execution_epoch=epoch,**kwargs)


def test_two_stage_ancestry_preserves_original_baseline_identity():
    assert check([edge(1),edge(2)])
    assert not check([edge(2)])
    assert not check([edge(1),edge(1),edge(2)])


@pytest.mark.parametrize('mutation',['snapshot','receipt','epoch','worker','node','kind','future_stop'])
def test_changed_recovery_edge_cannot_authorize_historical_facts(mutation):
    rows=[edge(1),edge(2)]
    row=rows[1]
    if mutation=='snapshot': row['snapshot_payload']['state']['epoch']=99
    elif mutation=='receipt': row['cleanup_receipt']['resources_quiescent']=1
    elif mutation=='epoch': row['source_epoch']=0
    elif mutation=='worker': row['command_worker']='foreign'
    elif mutation=='node': row['storage_node_id']='other'
    elif mutation=='kind': row['command_kind']='StartTask'
    else:
        row['command_kind']='StopTask'
        row['command_source_epoch']=99
    assert not check(rows)


def test_only_locked_authorization_may_read_tentative_last_edge():
    rows=[edge(1),edge(2)]
    rows[1].update(command_status='accepted',command_worker=None,command_epoch=None)
    assert not check(rows)
    assert check(rows,allow_pending_last=True)
    rows[0].update(command_status='accepted',command_worker=None,command_epoch=None)
    assert not check(rows,allow_pending_last=True)


def test_cancel_command_may_keep_older_observation_epoch():
    row=edge(1)
    row.update(command_kind='StopTask',command_source_epoch=0)
    assert check([row],epoch=2)
