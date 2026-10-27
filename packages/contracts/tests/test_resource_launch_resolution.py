"""Resolution contracts describe trusted launch fences; they do not operate them."""
import copy
import json

import pytest
from pydantic import ValidationError
from workmate_contracts.node_evidence import ResourceLaunchResolution
from test_node_evidence import payload,parse


def resolution(outcome='started',launch_state='reserved'):
    registration=copy.deepcopy(payload()['observations'][0]['registration'])
    registration.update(resource_id='child',registration_seq=2,kind='process',launch_state=launch_state,containment_ref=None)
    return {'registration':registration,'authority_id':'host-supervisor','outcome':outcome,
        'containment_ref':'container-incarnation' if outcome=='started' else None,
        'observation_ref':'launch-measurement','launch_fence_ref':'irrevocable-launch-fence',
        'observed_at':'2026-09-16T03:59:59Z'}


def evidence(outcome='started',launch_state='reserved'):
    data=payload()
    result=resolution(outcome,launch_state)
    data['registered_through_seq']=2
    data['observations'].append({'registration':copy.deepcopy(result['registration']),'resolution':result,
        'state':'terminated' if outcome=='started' else 'not_started','observation_ref':'exit-measurement'})
    return data


@pytest.mark.parametrize('outcome',['started','not_started'])
@pytest.mark.parametrize('launch_state',['reserved','unknown'])
def test_resolved_local_launches_can_form_complete_evidence(outcome,launch_state):
    value=parse(evidence(outcome,launch_state))
    result=value.observations[1].resolution
    assert result.outcome==outcome
    assert ResourceLaunchResolution.model_validate_json(result.model_dump_json())==result
    with pytest.raises(ValidationError): result.launch_fence_ref='replacement'
    assert len(result.digest())==64


@pytest.mark.parametrize('field,value',[('launch_fence_ref',None),('launch_fence_ref',' '),
    ('observation_ref',None),('observed_at','2026-09-16T03:59:59'),('outcome','unknown'),
    ('containment_ref',None),('authority_id','')])
def test_resolution_requires_bounded_measured_fence_and_started_containment(field,value):
    data=resolution();data[field]=value
    with pytest.raises(ValidationError): ResourceLaunchResolution.model_validate_json(json.dumps(data))


@pytest.mark.parametrize('kind,state',[('external_job','reserved'),('process','started')])
def test_resolution_cannot_rewrite_started_resources_or_resolve_remote_jobs(kind,state):
    data=resolution();data['registration'].update(kind=kind,launch_state=state,containment_ref='container-incarnation',original_tool_call_id='call')
    with pytest.raises(ValidationError): ResourceLaunchResolution.model_validate_json(json.dumps(data))


def test_not_started_resolution_forbids_containment_claim():
    data=resolution('not_started');data['containment_ref']='container-incarnation'
    with pytest.raises(ValidationError): ResourceLaunchResolution.model_validate_json(json.dumps(data))


@pytest.mark.parametrize('change',['registration','authority','future','containment','state','missing_resolution'])
def test_resolution_must_bind_original_registration_authority_time_and_observation(change):
    data=evidence();observation=data['observations'][1];result=observation['resolution']
    if change=='registration': result['registration']['resource_id']='different'
    elif change=='authority': result['authority_id']='other-authority'
    elif change=='future': result['observed_at']='2026-09-16T04:00:01Z'
    elif change=='containment': result['containment_ref']='replacement-container'
    elif change=='state': observation['state']='not_started'
    else: del observation['resolution']
    with pytest.raises(ValidationError): parse(data)


def test_not_started_observation_requires_matching_not_started_resolution():
    data=evidence('not_started');data['observations'][1]['state']='terminated'
    with pytest.raises(ValidationError): parse(data)
    data['conclusion']='unknown'
    with pytest.raises(ValidationError): parse(data)


def test_never_started_root_cannot_turn_empty_execution_into_quiescence():
    data=evidence('not_started')
    data['observations']=data['observations'][1:];data['registered_through_seq']=1
    observation=data['observations'][0]
    for registration in (observation['registration'],observation['resolution']['registration']):
        registration.update(kind='session_infrastructure',registration_seq=1)
    with pytest.raises(ValidationError): parse(data)
    observation['resolution'].update(outcome='started',containment_ref='container-incarnation')
    observation['state']='terminated'
    assert parse(data).conclusion=='confirmed'


def test_unknown_evidence_does_not_allow_foreign_or_future_resolution():
    data=evidence();data['conclusion']='unknown'
    data['observations'][1]['resolution']['authority_id']='foreign'
    with pytest.raises(ValidationError): parse(data)


def test_not_started_cannot_be_asserted_without_launch_resolution():
    data=evidence('not_started')
    del data['observations'][1]['resolution']
    data['conclusion']='unknown'
    with pytest.raises(ValidationError): parse(data)
