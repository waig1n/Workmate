"""Node measurements are strict immutable values, not recovery authorization."""
import copy
import json
from datetime import datetime,timezone
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError
from workmate_contracts import snapshot_digest
from workmate_contracts.node_evidence import (
    NodeContainment,WorkerIncarnation,WorkspaceGeneration,ResourceRegistration,
    PermissionParkedBoundary,ResourceObservation,NodeQuiescenceEvidence,
)


def payload():
    return {
        'evidence_id':'evidence-1','authority_id':'host-supervisor',
        'worker':{'worker_id':'worker-incarnation','storage_node_id':'node',
            'node_authority_id':'host-supervisor','host_boot_id':'11111111-2222-3333-4444-555555555555',
            'host_pid':1234,'process_start_ticks':9876,
            'containment':{'kind':'container','runtime_instance_id':'container-incarnation',
                'generation':'runtime-generation','pid_namespace_inode':4026532480}},
        'workspace':{'session_id':'session','conversation_id':'conversation','storage_node_id':'node',
            'generation':'workspace-generation','volume_id':'persistent-volume','workspace_id':'workspace'},
        'task_id':'task','execution_epoch':1,
        'boundary':{'snapshot_id':'snapshot','snapshot_sha256':'a'*64,'state_version':1,'runtime_seq':10,
            'reply_id':'reply','request_version':'approval-d2de6c43554527cadbbbaf1f4d0af446','pending_tool_call_ids':['call-1']},
        'registered_through_seq':1,'ledger_sha256':'c'*64,
        'observed_at':'2026-09-16T04:00:00Z','worker_state':'terminated',
        'containment_state':'terminated','conclusion':'confirmed',
        'observations':[{'registration':{'resource_id':'root','worker_id':'worker-incarnation',
            'session_id':'session','task_id':'task','execution_epoch':1,'registration_seq':1,
            'kind':'session_infrastructure','launch_state':'started',
            'containment_ref':'container-incarnation'},'state':'terminated','observation_ref':'measurement-1'}],
    }


def parse(value):
    return NodeQuiescenceEvidence.model_validate_json(json.dumps(value))


def set_path(value,path,replacement):
    current=value
    for key in path[:-1]: current=current[key]
    current[path[-1]]=replacement


def test_complete_evidence_roundtrips_and_each_object_has_canonical_digest():
    evidence=parse(payload())
    assert NodeQuiescenceEvidence.model_validate_json(evidence.model_dump_json())==evidence
    objects=(evidence,evidence.worker,evidence.worker.containment,evidence.workspace,evidence.boundary,
        evidence.observations[0],evidence.observations[0].registration)
    assert {type(item) for item in objects}=={NodeQuiescenceEvidence,WorkerIncarnation,NodeContainment,
        WorkspaceGeneration,PermissionParkedBoundary,ResourceObservation,ResourceRegistration}
    for item in objects:
        assert item.digest()==snapshot_digest(item.model_dump(mode='json'))
    assert evidence.observed_at.tzinfo is not None
    with pytest.raises(ValidationError): evidence.worker.host_pid=2222
    assert isinstance(evidence.observations,tuple)
    assert isinstance(evidence.boundary.pending_tool_call_ids,tuple)


@pytest.mark.parametrize(('path','value'),[
    (('worker','host_pid'),True),
    (('worker','host_pid'),0),
    (('worker','host_boot_id'),'not-a-boot-id'),
    (('worker','process_start_ticks'),-1),
    (('worker','containment','pid_namespace_inode'),0),
    (('worker','worker_id'),'  '),
    (('workspace','storage_node_id'),'other'),
    (('authority_id',),'foreign-supervisor'),
    (('execution_epoch',),0),
    (('boundary','kind'),'external_execution'),
    (('boundary','status'),'running'),
    (('boundary','snapshot_sha256'),'a'*63),
    (('boundary','request_version'),' '),
    (('boundary','pending_tool_call_ids'),[]),
    (('boundary','pending_tool_call_ids'),['call','call']),
    (('observed_at',),'2026-09-16T04:00:00'),
    (('observations',0,'registration','worker_id'),'other'),
    (('observations',0,'registration','task_id'),'other'),
    (('observations',0,'registration','session_id'),'other'),
    (('observations',0,'registration','execution_epoch'),2),
    (('observations',0,'registration','containment_ref'),'unrelated-container'),
    (('observations',0,'registration','containment_ref'),None),
    (('observations',0,'registration','kind'),'process'),
    (('observations',0,'state'),'completed'),
    (('observations',0,'observation_ref'),None),
])
def test_contradictory_or_unbound_confirmed_evidence_is_rejected(path,value):
    data=payload()
    set_path(data,path,value)
    with pytest.raises(ValidationError): parse(data)


@pytest.mark.parametrize(('path','value'),[
    (('worker_state',),'active'),(('worker_state',),'unknown'),
    (('containment_state',),'active'),(('containment_state',),'unknown'),
    (('observations',0,'state'),'active'),(('observations',0,'state'),'unknown'),
    (('observations',0,'registration','launch_state'),'reserved'),
    (('observations',0,'registration','launch_state'),'unknown'),
])
def test_unknown_or_live_state_may_be_observed_but_never_confirms_quiescence(path,value):
    data=payload()
    set_path(data,path,value)
    with pytest.raises(ValidationError): parse(data)
    data['conclusion']='unknown'
    assert parse(data).conclusion=='unknown'


def test_remote_jobs_are_outside_this_recovery_scope_even_when_claimed_completed():
    data=payload()
    registration=data['observations'][0]['registration']
    registration.update(kind='external_job',original_tool_call_id='remote-call',containment_ref=None)
    data['observations'][0]['state']='completed'
    with pytest.raises(ValidationError): parse(data)
    data['conclusion']='unknown'
    assert parse(data).observations[0].registration.kind=='external_job'
    del registration['original_tool_call_id']
    with pytest.raises(ValidationError): parse(data)


@pytest.mark.parametrize('watermark',[0,2,9_223_372_036_854_775_807])
def test_empty_missing_or_extreme_watermarks_cannot_create_complete_evidence(watermark):
    data=payload()
    data['registered_through_seq']=watermark
    if watermark==0: data['observations']=[]
    # The huge watermark must be rejected by bounded comparison, not expanded
    # into a list/range allocation. Input contains only one observation.
    with pytest.raises(ValidationError): parse(data)


def test_duplicate_resources_and_duplicate_sequences_are_rejected():
    data=payload()
    data['registered_through_seq']=2
    data['observations'].append(copy.deepcopy(data['observations'][0]))
    with pytest.raises(ValidationError): parse(data)
    data['observations'][1]['registration']['resource_id']='second'
    with pytest.raises(ValidationError): parse(data)
    data['observations'][1]['registration']['registration_seq']=2
    assert len(parse(data).observations)==2


def test_unknown_can_describe_an_empty_observation_but_cannot_claim_database_frozen():
    data=payload()
    data.update(conclusion='unknown',registered_through_seq=0,observations=[])
    assert parse(data).observations==()
    data['ledger_closed']=True
    with pytest.raises(ValidationError): parse(data)


@pytest.mark.parametrize('path',['relative','/group/../other','/group/./other','/group\\other','/group\nother'])
def test_cgroup_paths_cannot_alias_other_groups(path):
    data=payload()
    data['worker']['containment'].update(kind='cgroup',cgroup_path=path)
    with pytest.raises(ValidationError): parse(data)


def test_cgroup_identity_requires_generation_and_host_path():
    data=payload()
    data['worker']['containment']['kind']='cgroup'
    with pytest.raises(ValidationError): parse(data)
    data['worker']['containment']['cgroup_path']='/system.slice/worker.scope'
    assert parse(data).worker.containment.kind=='cgroup'
    del data['worker']['containment']['generation']
    with pytest.raises(ValidationError): parse(data)


def test_evidence_size_bound_is_enforced_after_shape_validation():
    data=payload()
    observation=data['observations'][0]
    data['observations']=[]
    for index in range(800):
        entry=copy.deepcopy(observation)
        entry['registration'].update(resource_id=f'{index}-'+('r'*100),registration_seq=index+1)
        data['observations'].append(entry)
    data['registered_through_seq']=800
    with pytest.raises(ValidationError,match='payload bound'): parse(data)


def test_protocol_and_extra_fields_fail_closed_and_digest_binds_generation():
    data=payload()
    original=parse(data)
    data['workspace']['generation']='replacement-generation'
    assert parse(data).digest()!=original.digest()
    data['protocol_version']=2
    with pytest.raises(ValidationError): parse(data)
    data['protocol_version']=1
    data['recovery_authorized']=True
    with pytest.raises(ValidationError): parse(data)


def test_node_contract_imports_no_product_runtime_or_engine_packages():
    root=Path(__file__).resolve().parents[1]/'src'
    script='''
import sys
from importlib.abc import MetaPathFinder
sys.path.insert(0,sys.argv[1])
class RejectApp(MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] in {'workmate_product','workmate_runtime','agentscope','sqlalchemy','app'}:
            raise AssertionError('Forbidden contract dependency: '+fullname)
sys.meta_path.insert(0,RejectApp())
from workmate_contracts.node_evidence import NodeQuiescenceEvidence
assert NodeQuiescenceEvidence.model_json_schema()['title']=='NodeQuiescenceEvidence'
'''
    result=subprocess.run([sys.executable,'-I','-c',script,str(root)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
