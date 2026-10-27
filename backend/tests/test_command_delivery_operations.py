"""Operator redrive uses real RBAC and immutable Product intent on isolated PG."""
import json
import pytest

from test_durable_projection import projection_database
from test_control_commands import parked, admit, stop
from workmate_contracts import Command
from workmate_product.application.commands import accept_start, CommandRejected
from workmate_product.application.command_delivery_operations import redrive_command
from workmate_product.persistence.repositories.command_delivery_operations import inspect_deliveries
from workmate_product.authorization.permissions import ROOT_ADMIN_PERMISSIONS


async def prepare(sessions,admin):
    await admin.execute("""INSERT INTO users(id,email,normalized_email,password_hash,display_name,status) VALUES ('op','op@example.invalid','op@example.invalid','x','Operator','active');
        INSERT INTO roles(id,key,display_name) VALUES ('admin-role','admin','Admin');
        INSERT INTO role_bindings(user_id,role_id,scope_id,include_descendants) VALUES ('op','admin-role','scope',true);
        UPDATE agents SET created_by='u';""")
    for n,permission in enumerate(ROOT_ADMIN_PERMISSIONS):
        await admin.execute('INSERT INTO permissions(id,key,description) VALUES ($1,$2,$2)',f'p{n}',permission)
        await admin.execute('INSERT INTO role_permissions(id,role_id,permission_id) VALUES ($1,$2,$3)',f'rp{n}','admin-role',f'p{n}')


async def start(sessions,admin):
    await prepare(sessions,admin)
    await admin.execute("""DELETE FROM runtime_task_executions; UPDATE tasks SET status='created';
        UPDATE conversations SET active_product_task_id=NULL;
        UPDATE runtime_sessions SET active_task_id=NULL,status='accepted',execution_epoch=0,reply_id=NULL;""")
    command=Command(command_id='start',kind='StartTask',conversation_id='c',runtime_session_id='s',product_task_id='t',payload={'input':'private prompt'})
    async def current_access(db,actor,agent):
        from workmate_product.application.authorization import AccessDecisionService
        return await AccessDecisionService(db).can_use_agent(actor,agent)
    async with sessions.begin() as db:
        await accept_start(db,command,actor_user_id='u',storage_node_id='node',authorize_agent=current_access)
    await admin.execute("UPDATE command_deliveries SET status='quarantined',error_code='queue_unavailable'")


async def drive(sessions,command_id='start',**changes):
    args={'operator_id':'op','expected_generation':1,'storage_node_id':'node','reason':'Redis service recovered',**changes}
    async with sessions.begin() as db:
        return await redrive_command(db,command_id,**args)


async def test_start_redrive_reauthorizes_preserves_identity_and_audits(projection_database):
    sessions,admin=projection_database
    await start(sessions,admin)
    assert await drive(sessions)==2
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==1
    rows=await admin.fetch('SELECT status,retry_origin_generation FROM command_deliveries ORDER BY delivery_generation')
    assert [tuple(row) for row in rows]==[('quarantined',1),('pending',2)]
    audit=json.loads(await admin.fetchval("SELECT metadata FROM security_audit_events WHERE event_type='command_delivery.redrive'"))
    assert audit['previous_generation']==1 and audit['delivery_generation']==2
    async with sessions() as db:
        report=await inspect_deliveries(db)
    assert 'payload' not in report[0] and 'private prompt' not in str(report)
    with pytest.raises(CommandRejected):
        await drive(sessions)


@pytest.mark.parametrize('mutation',[
    "DELETE FROM role_bindings", "UPDATE users SET status='disabled' WHERE id='u'",
    "UPDATE users SET status='disabled' WHERE id='op'", "UPDATE agents SET created_by=NULL",
    "UPDATE runtime_commands SET status='claimed'", "UPDATE runtime_commands SET status='indeterminate'",
    "UPDATE runtime_commands SET payload='{}'", "UPDATE runtime_sessions SET owner_id='lost',lease_until=now()-interval '1 hour'",
    "UPDATE runtime_sessions SET storage_node_id='other'", "UPDATE tasks SET status='running'",
])
async def test_unsafe_or_unauthorized_redrive_leaves_quarantine(projection_database,mutation):
    sessions,admin=projection_database
    await start(sessions,admin)
    await admin.execute(mutation)
    with pytest.raises(CommandRejected):
        await drive(sessions)
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries')==1
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='command_delivery.redrive'")==0


async def test_resume_redrive_checks_original_decision_and_current_boundary(projection_database):
    sessions,admin=projection_database
    await prepare(sessions,admin)
    cmd=await parked(sessions,admin)
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker',lease_until=now()+interval '1 hour'")
    await admit(sessions,cmd)
    await admin.execute("UPDATE command_deliveries SET status='quarantined'")
    assert await drive(sessions,'resume')==2
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_decision'")==1


async def test_stop_redrive_remains_allowed_after_agent_use_revoked(projection_database):
    sessions,admin=projection_database
    await prepare(sessions,admin)
    await parked(sessions,admin)
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker',lease_until=now()+interval '1 hour'")
    await admit(sessions,stop())
    await admin.execute("UPDATE agents SET created_by=NULL; UPDATE command_deliveries SET status='quarantined'")
    assert await drive(sessions,'stop')==2
    assert await admin.fetchval('SELECT status FROM tasks')=='cancelling'


@pytest.mark.parametrize('mutation',[
    "UPDATE runtime_sessions SET snapshot_id=NULL", "UPDATE tasks SET last_decision_version='other'",
    "UPDATE runtime_commands SET payload=jsonb_set(payload,'{decisions,0,approved}','false')",
    "DELETE FROM task_events WHERE event_type='hitl_decision'",
    "UPDATE task_events SET created_at=now()-interval '2 days' WHERE event_type='hitl_request'",
    "UPDATE runtime_sessions SET owner_id=NULL,lease_until=NULL",
])
async def test_resume_stale_consent_or_unknown_resources_cannot_be_redriven(projection_database,mutation):
    sessions,admin=projection_database
    await prepare(sessions,admin)
    cmd=await parked(sessions,admin)
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker',lease_until=now()+interval '1 hour'")
    await admit(sessions,cmd)
    await admin.execute("UPDATE command_deliveries SET status='quarantined'")
    await admin.execute(mutation)
    with pytest.raises(CommandRejected):
        await drive(sessions,'resume')
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries')==1


async def test_redrive_failure_or_check_rollback_includes_security_audit(projection_database):
    sessions,admin=projection_database
    await start(sessions,admin)
    async with sessions() as db:
        async with db.begin():
            assert await redrive_command(db,'start',operator_id='op',expected_generation=1,
                storage_node_id='node',reason='check only')==2
            await db.rollback()
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries')==1
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='command_delivery.redrive'")==0


async def test_redrive_resets_bounded_route_budget_without_reusing_generation(projection_database):
    from workmate_product.persistence.repositories.command_deliveries import reroute
    sessions,admin=projection_database
    await start(sessions,admin)
    await admin.execute('UPDATE command_deliveries SET delivery_generation=20')
    assert await drive(sessions,expected_generation=20)==21
    async with sessions.begin() as db:
        assert await reroute(db,'start',expected_generation=21,max_generations=2)==22
        assert await reroute(db,'start',expected_generation=22,max_generations=2) is None
    assert await admin.fetchval('SELECT status FROM command_deliveries WHERE delivery_generation=22')=='quarantined'


async def test_readonly_inspection_works_with_filters(projection_database):
    from sqlalchemy import text
    sessions,admin=projection_database
    await start(sessions,admin)
    async with sessions.begin() as db:
        await db.execute(text('SET TRANSACTION READ ONLY'))
        assert len(await inspect_deliveries(db,command_id='start',limit=1))==1
        assert await inspect_deliveries(db,command_id='unknown')==[]


async def test_cli_inspect_and_redrive_check_use_local_schema_without_persistent_changes(projection_database,monkeypatch):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import text
    import sqlalchemy.ext.asyncio
    sessions,admin=projection_database
    await start(sessions,admin)
    path=Path(__file__).resolve().parents[1]/'scripts/command_deliveries.py'
    spec=importlib.util.spec_from_file_location('command_delivery_cli',path)
    cli=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    engine=sessions.kw['bind']
    monkeypatch.setattr(sqlalchemy.ext.asyncio,'create_async_engine',lambda url:engine)
    monkeypatch.setenv('LOCAL_CLI_TEST_DB','postgresql://test:never-print@example.invalid/test')
    from workmate_product.persistence.repositories.command_deliveries import quarantine_message
    async with sessions.begin() as db:
        await quarantine_message(db,'workmate:test:node:node:ready','1-0','invalid_envelope')
    report=await cli.run(cli.parser().parse_args(['--database-env','LOCAL_CLI_TEST_DB','inspect']))
    assert report['mode']=='inspect' and len(report['rows'])==1
    assert report['quarantined_messages'][0]['command_id'] is None
    assert report['quarantined_messages'][0]['error_code']=='invalid_envelope'
    result=await cli.run(cli.parser().parse_args(['--database-env','LOCAL_CLI_TEST_DB','redrive',
        '--command-id','start','--operator-id','op','--generation','1','--node','node','--reason','local test','--check']))
    assert result['committed'] is False and result['delivery_generation']==2
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries')==1


async def test_already_admitted_stop_can_be_redriven_after_owner_disabled(projection_database):
    sessions,admin=projection_database
    await prepare(sessions,admin)
    await parked(sessions,admin)
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker',lease_until=now()+interval '1 hour'")
    await admit(sessions,stop())
    await admin.execute("UPDATE users SET status='disabled' WHERE id='u'; UPDATE command_deliveries SET status='quarantined'")
    assert await drive(sessions,'stop')==2


async def test_approval_expiry_stop_redrive_rechecks_policy_receipt(projection_database):
    from test_approval_expiry import age,expire
    sessions,admin=projection_database
    await prepare(sessions,admin)
    await parked(sessions,admin)
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker',lease_until=now()+interval '1 hour'")
    await age(admin)
    assert await expire(sessions)
    command_id=await admin.fetchval('SELECT id FROM runtime_commands')
    await admin.execute("UPDATE command_deliveries SET status='quarantined'")
    assert await drive(sessions,command_id)==2


async def test_account_disable_stop_redrive_requires_same_durable_disable_cycle(projection_database):
    from test_disabled_user_stop import prepare as disable_prepare
    from app.services.user_management import UserManagementService
    from app.services.product_runtime import ProductRuntime
    from workmate_product.persistence.models import User
    sessions,admin=projection_database
    target=await disable_prepare(sessions,admin)
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker',lease_until=now()+interval '1 hour'")
    async with sessions() as db:
        await UserManagementService(db,ProductRuntime(sessions,None),actor_id='u').disable_user(
            actor=await db.get(User,'u'),user=await db.get(User,target))
    command_id=await admin.fetchval('SELECT id FROM runtime_commands')
    await admin.execute("UPDATE command_deliveries SET status='quarantined'")
    assert await drive(sessions,command_id,operator_id='u')==2
    await admin.execute("UPDATE command_deliveries SET status='quarantined' WHERE delivery_generation=2; UPDATE users SET disabled_at=now() WHERE id='other'")
    with pytest.raises(CommandRejected,match='disable authorization receipt'):
        await drive(sessions,command_id,operator_id='u',expected_generation=2)


@pytest.mark.parametrize('lost_proof',[False,True])
async def test_full_access_redrive_revalidates_saved_sandbox_without_new_grant(projection_database,lost_proof):
    from test_full_access_upgrade import prepared,accept
    sessions,admin=projection_database
    await prepare(sessions,admin)
    command=await prepared(sessions,admin)
    await accept(sessions,command)
    await admin.execute("UPDATE runtime_sessions SET lease_until=now()+interval '1 hour'; UPDATE command_deliveries SET status='quarantined'")
    if lost_proof:
        await admin.execute("UPDATE runtime_sessions SET owner_id='different-worker'")
        with pytest.raises(CommandRejected,match='sandbox isolation'):
            await drive(sessions,'resume')
    else:
        assert await drive(sessions,'resume')==2
    assert await admin.fetchval('SELECT count(*) FROM runtime_permission_upgrades')==1


async def test_malformed_quarantine_inspection_is_readonly_bounded_and_visible_without_command(projection_database):
    from sqlalchemy import text
    from workmate_product.persistence.repositories.command_deliveries import quarantine_message
    from workmate_product.persistence.repositories.command_delivery_operations import inspect_quarantined_messages
    sessions,admin=projection_database
    async with sessions.begin() as db:
        await quarantine_message(db,'workmate:test:node:node:ready','1-0','invalid_envelope')
        await quarantine_message(db,'workmate:test:node:node:ready','2-0','unknown_command','missing-command')
    async with sessions.begin() as db:
        await db.execute(text('SET TRANSACTION READ ONLY'))
        rows=await inspect_quarantined_messages(db,limit=10)
        assert len(rows)==2 and any(row['command_id'] is None for row in rows)
        assert set(rows[0])=={'stream_key','message_id','command_id','error_code','created_at'}
        assert len(await inspect_quarantined_messages(db,limit=1))==1
        filtered=await inspect_quarantined_messages(db,command_id='missing-command',limit=10)
        assert len(filtered)==1 and filtered[0]['message_id']=='2-0'
        with pytest.raises(ValueError):
            await inspect_quarantined_messages(db,limit=0)
    assert await admin.fetchval('SELECT count(*) FROM command_delivery_quarantines')==2
