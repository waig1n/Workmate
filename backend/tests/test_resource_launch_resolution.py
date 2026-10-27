"""Durable launch outcomes never rewrite reservations or grant execution."""
import asyncio
import asyncpg
from datetime import datetime, timezone
import pytest
from workmate_contracts.node_evidence import ResourceLaunchResolution, ResourceObservation
from workmate_product.application.node_recovery import record_launch_resolution, resource_ledger_digest
from workmate_product.application.commands import CommandRejected, CommandConflict
from test_durable_projection import projection_database  # noqa: F401
from test_node_recovery_evidence import make_case, verified, denied, assert_no_transfer


def resolution(case, resource, outcome='started'):
    return ResourceLaunchResolution(registration=resource, authority_id=case.worker.node_authority_id,
        outcome=outcome, containment_ref=case.worker.containment.runtime_instance_id if outcome=='started' else None,
        observation_ref='launch-observation', launch_fence_ref='irrevocable-launch-fence',
        observed_at=datetime.now(timezone.utc))


async def record(case, value, verify=verified):
    async with case.sessions.begin() as db:
        return await record_launch_resolution(db,value,verify_authority=verify)


def proof(case, boundary, value):
    evidence=case.evidence(boundary,conclusion='unknown')
    observations=tuple(ResourceObservation(registration=r,
        state=('not_started' if value.outcome=='not_started' else 'terminated') if r==value.registration else 'terminated',
        observation_ref='resource-exit',resolution=value if r==value.registration else None) for r in case.registrations)
    return type(evidence).model_validate({**evidence.model_dump(), 'conclusion':'confirmed',
        'observations':observations, 'observed_at':datetime.now(timezone.utc),
        'ledger_sha256':resource_ledger_digest(case.worker,case.workspace,'t',1,case.registrations,resolutions=[value])})


@pytest.mark.parametrize('launch_state',['reserved','unknown'])
@pytest.mark.parametrize('outcome',['started','not_started'])
async def test_resolved_launch_can_freeze_without_rewriting_registration(projection_database,launch_state,outcome):
    case=await make_case(projection_database).prepare()
    resource=case.resource('child',2,launch_state=launch_state)
    await case.add(resource)
    boundary=await case.park()
    await case.admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    value=resolution(case,resource,outcome)
    assert await record(case,value)==value.digest()
    assert await record(case,value)==value.digest()
    await case.accept(proof(case,boundary,value))
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_resource_launch_resolutions')==1
    assert await case.admin.fetchval("SELECT payload->>'launch_state' FROM runtime_resource_registrations WHERE resource_id='child'")==launch_state
    await assert_no_transfer(case.admin)


@pytest.mark.parametrize('fault',['authority','registration','epoch','future','denied'])
async def test_invalid_resolution_is_atomic(projection_database,fault):
    case=await make_case(projection_database).prepare()
    resource=case.resource('child',2,launch_state='reserved')
    await case.add(resource)
    value=resolution(case,resource)
    if fault=='authority': value=value.model_copy(update={'authority_id':'other'})
    if fault=='registration': value=value.model_copy(update={'registration':resource.model_copy(update={'resource_id':'missing'})})
    if fault=='epoch': value=value.model_copy(update={'registration':resource.model_copy(update={'execution_epoch':2})})
    if fault=='future':
        from datetime import timedelta
        value=value.model_copy(update={'observed_at':value.observed_at+timedelta(days=1)})
    with pytest.raises(CommandRejected):
        await record(case,value,denied if fault=='denied' else verified)
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_resource_launch_resolutions')==0


async def test_conflicting_concurrent_resolutions_only_accept_one(projection_database):
    case=await make_case(projection_database).prepare()
    resource=case.resource('child',2,launch_state='reserved')
    await case.add(resource)
    values=[resolution(case,resource,outcome) for outcome in ('started','not_started')]
    results=await asyncio.gather(*(record(case,v) for v in values),return_exceptions=True)
    assert sum(isinstance(r,CommandConflict) for r in results)==1
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_resource_launch_resolutions')==1


async def test_unpersisted_resolution_cannot_freeze(projection_database):
    case=await make_case(projection_database).prepare()
    resource=case.resource('child',2,launch_state='reserved')
    await case.add(resource)
    boundary=await case.park()
    with pytest.raises(CommandRejected):
        await case.accept(proof(case,boundary,resolution(case,resource)))
    assert not await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers')


async def test_resolution_is_append_only_and_frozen_duplicate_is_idempotent(projection_database):
    case=await make_case(projection_database).prepare()
    resource=case.resource('child',2,launch_state='reserved')
    await case.add(resource)
    boundary=await case.park()
    value=resolution(case,resource)
    await record(case,value)
    await case.accept(proof(case,boundary,value))
    assert await record(case,value)==value.digest()
    for statement in ('DELETE FROM runtime_resource_launch_resolutions',
                      "UPDATE runtime_resource_launch_resolutions SET digest='changed'"):
        with pytest.raises(asyncpg.PostgresError,match='immutable'):
            await case.admin.execute(statement)


async def test_verifier_exception_rolls_back_nested_work_even_when_caller_commits(projection_database):
    from sqlalchemy import text
    case=await make_case(projection_database).prepare()
    resource=case.resource('child',2,launch_state='reserved')
    await case.add(resource)
    before=await case.admin.fetchval('SELECT title FROM tasks')
    async def unavailable(db,value):
        await db.execute(text("UPDATE tasks SET title='must roll back' WHERE id='t'"))
        raise RuntimeError('trusted observation unavailable')
    async with case.sessions.begin() as db:
        with pytest.raises(RuntimeError,match='trusted observation unavailable'):
            await record_launch_resolution(db,resolution(case,resource),verify_authority=unavailable)
    assert await case.admin.fetchval('SELECT title FROM tasks')==before
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_resource_launch_resolutions')==0
    assert await case.admin.fetchval('SELECT resource_seq FROM runtime_resource_ledgers')==2
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


async def test_freeze_observation_must_postdate_resolution_persistence(projection_database):
    case=await make_case(projection_database).prepare()
    resource=case.resource('child',2,launch_state='reserved')
    await case.add(resource)
    boundary=await case.park()
    value=resolution(case,resource)
    earlier=proof(case,boundary,value)
    await record(case,value)
    created=await case.admin.fetchval('SELECT created_at FROM runtime_resource_launch_resolutions')
    assert earlier.observed_at<created
    with pytest.raises(CommandRejected):
        await case.accept(earlier)
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False
    await case.accept(proof(case,boundary,value))
    await assert_no_transfer(case.admin)


async def test_resolution_and_freeze_race_preserves_receipt_dependency(projection_database):
    case=await make_case(projection_database).prepare()
    resource=case.resource('child',2,launch_state='reserved')
    await case.add(resource)
    boundary=await case.park()
    value=resolution(case,resource)
    premature=proof(case,boundary,value)
    results=await asyncio.gather(record(case,value),case.accept(premature),return_exceptions=True)
    assert results[0]==value.digest()
    assert isinstance(results[1],CommandRejected)
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_resource_launch_resolutions')==1
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_node_quiescence_evidence')==0
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False
    await case.accept(proof(case,boundary,value))
    await assert_no_transfer(case.admin)


@pytest.mark.parametrize('fault',['containment','registered_containment','sequence','session','kind','launch_state','fence'])
async def test_resolution_cannot_change_registered_launch_identity(projection_database,fault):
    case=await make_case(projection_database).prepare()
    resource=case.resource('child',2,launch_state='reserved')
    await case.add(resource)
    value=resolution(case,resource)
    if fault=='containment':
        value=value.model_copy(update={'containment_ref':'replacement-container'})
    elif fault=='fence':
        await record(case,value)
        value=value.model_copy(update={'launch_fence_ref':'replacement-fence'})
    else:
        field,replacement={'registered_containment':('containment_ref','replacement-container'),
            'sequence':('registration_seq',3),'session':('session_id','other-session'),
            'kind':('kind','session_infrastructure'),'launch_state':('launch_state','unknown')}[fault]
        value=value.model_copy(update={'registration':resource.model_copy(update={field:replacement})})
    with pytest.raises((CommandRejected,CommandConflict)):
        await record(case,value)
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_resource_launch_resolutions')==(1 if fault=='fence' else 0)
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


@pytest.mark.parametrize('fault',['replacement_resolution','missing_digest_resolution','omitted_resource'])
async def test_frozen_proof_must_include_exact_persisted_resolution(projection_database,fault):
    case=await make_case(projection_database).prepare()
    resource=case.resource('child',2,launch_state='reserved')
    await case.add(resource)
    boundary=await case.park()
    value=resolution(case,resource)
    await record(case,value)
    candidate=proof(case,boundary,value)
    if fault=='replacement_resolution':
        replacement=value.model_copy(update={'observation_ref':'different-measurement'})
        candidate=proof(case,boundary,replacement)
    elif fault=='missing_digest_resolution':
        candidate=candidate.model_copy(update={'ledger_sha256':resource_ledger_digest(case.worker,case.workspace,'t',1,case.registrations)})
    else:
        root=case.registrations[0]
        candidate=candidate.model_copy(update={'registered_through_seq':1,'observations':candidate.observations[:1],
            'ledger_sha256':resource_ledger_digest(case.worker,case.workspace,'t',1,[root])})
    with pytest.raises(CommandRejected):
        await case.accept(candidate)
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_resource_launch_resolutions')==1
