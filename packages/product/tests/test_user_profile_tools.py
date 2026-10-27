"""User data tools enforce live actor and per-Scope directory authorization."""
import pytest
import pytest_asyncio
from test_execution_context import database, record
from test_platform_mutations import request
from workmate_product.application.platform_reads import execute_platform_read
from workmate_product.application.platform_types import PlatformToolError
from workmate_product.persistence.models import (
    User, Scope, ScopeClosure, ScopeMembership, Role, Permission, RolePermission, RoleBinding,
)


@pytest_asyncio.fixture
async def people_database(database):
    sessions, admin = database
    async with sessions.begin() as db:
        db.add_all([User(id=uid,email=f'{uid}@example.test',password_hash='secret',display_name=uid)
                    for uid in ('alice','bob','carol')])
        db.add(Scope(id='org',kind='organization',name='Company'))
        await db.flush()
        db.add_all([Scope(id='dept',kind='department',name='Department',parent_id='org'),
                    Scope(id='other',kind='department',name='Other',parent_id='org')])
        await db.flush()
        db.add(Scope(id='child',kind='department',name='Child',parent_id='dept'))
        await db.flush()
        db.add_all([ScopeClosure(ancestor_id=a,descendant_id=d,depth=depth) for a,d,depth in
                    [('org','org',0),('dept','dept',0),('other','other',0),('child','child',0),
                     ('org','dept',1),('org','other',1),('org','child',2),('dept','child',1)]])
        db.add_all([ScopeMembership(scope_id=sid,user_id=uid) for sid,uid in
                    [('org','u'),('org','alice'),('org','bob'),('org','carol'),
                     ('dept','alice'),('other','alice'),('child','bob'),('other','carol')]])
        db.add_all([Role(id='reader',key='reader',display_name='Reader'),
                    Permission(id='member-read',key='membership.read'),Permission(id='user-read',key='user.read')])
        await db.flush()
        db.add(RolePermission(id='rp',role_id='reader',permission_id='member-read'))
    await record(sessions)  # Normal Agent: platform_tool_profile=none.
    await admin.execute("""UPDATE tasks SET status='running',run_id='reply';
        UPDATE runtime_sessions SET owner_id='worker',execution_epoch=1,active_task_id='t',
          status='running',reply_id='reply',lease_until=clock_timestamp()+interval '60 seconds';
        UPDATE runtime_task_executions SET execution_epoch=1,status='running',reply_id='reply';""")
    yield sessions, admin


async def call(sessions, name='get_current_user', args=None, **changes):
    req=request().model_copy(update={'tool_name':name,'arguments':args or {},**changes})
    async with sessions.begin() as db:
        return await execute_platform_read(db,req,model_profiles=[])


async def grant(sessions, scope='dept', descendants=False):
    async with sessions.begin() as db:
        db.add(RoleBinding(user_id='u',role_id='reader',scope_id=scope,include_descendants=descendants))


@pytest.mark.asyncio
async def test_ordinary_agent_reads_self_only(people_database):
    sessions,_=people_database
    result=await call(sessions)
    assert result=={'id':'u','display_name':'User','email':'u@example.test','departments':[], 'readable_scopes':[]}
    own=await call(sessions,'list_scope_users',{'scope_id':'scope'})
    assert [p['id'] for p in own['items']]==['u']
    for scope in ('dept','org','other'):
        with pytest.raises(PlatformToolError) as exc:
            await call(sessions,'list_scope_users',{'scope_id':scope})
        assert exc.value.status_code==403


@pytest.mark.asyncio
async def test_department_grant_filters_before_paging_and_hides_other_memberships(people_database):
    sessions,admin=people_database
    await grant(sessions)
    args={'scope_id':'dept','include_descendants':True,'limit':1}
    result=await call(sessions,'list_scope_users',args)
    assert result['items']==[{'id':'alice','display_name':'alice','email':'alice@example.test',
                             'departments':[{'id':'dept','name':'Department'}]}]
    assert result['next_cursor'] is None
    assert [s['id'] for s in (await call(sessions))['readable_scopes']]==['dept']
    await admin.execute("UPDATE role_bindings SET include_descendants=true WHERE user_id='u'")
    first=await call(sessions,'list_scope_users',args)
    assert first['next_cursor'] is not None
    second=await call(sessions,'list_scope_users',{**args,'cursor':first['next_cursor']})
    assert [u['id'] for u in second['items']]==['bob']
    assert second['next_cursor'] is None
    assert (await call(sessions,'list_scope_users',{**args,'q':'%'}))['items']==[]
    await admin.execute("DELETE FROM role_bindings WHERE user_id='u'")
    with pytest.raises(PlatformToolError):
        await call(sessions,'list_scope_users',{**args,'cursor':first['next_cursor']})


@pytest.mark.asyncio
async def test_root_user_read_can_query_directory_but_department_user_read_cannot(people_database):
    sessions,admin=people_database
    await admin.execute("UPDATE role_permissions SET permission_id='user-read' WHERE id='rp'")
    await grant(sessions)
    with pytest.raises(PlatformToolError):
        await call(sessions,'list_scope_users',{'scope_id':'dept'})
    await admin.execute("UPDATE role_bindings SET scope_id='org' WHERE user_id='u'")
    result=await call(sessions,'list_scope_users',{'scope_id':'org'})
    assert {u['id'] for u in result['items']}=={'u','alice','bob','carol'}
    assert len((await call(sessions,'list_scope_users',{'scope_id':'dept'}))['items'])==1


@pytest.mark.asyncio
@pytest.mark.parametrize('name,args',[
    ('get_current_user',{'user_id':'alice'}),('list_scope_users',{}),
    ('list_scope_users',{'scope_id':'dept','limit':101}),
    ('list_scope_users',{'scope_id':'dept','limit':0}),
    ('list_scope_users',{'scope_id':'dept','include_descendants':'true'}),
    ('list_scope_users',{'scope_id':'dept','q':'x'*201}),
])
async def test_invalid_or_identity_overriding_arguments_fail(people_database,name,args):
    with pytest.raises(PlatformToolError) as exc:
        await call(people_database[0],name,args)
    assert exc.value.status_code==422


@pytest.mark.asyncio
@pytest.mark.parametrize('sql',[
    "UPDATE users SET status='disabled' WHERE id='u'",
    "UPDATE runtime_sessions SET owner_id='other'",
    "UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'",
    "UPDATE tasks SET status='cancelling'",
    "UPDATE agents SET tool_policy='{\"denied_tools\":[\"GET_CURRENT_USER\"]}'",
])
async def test_current_execution_and_policy_are_required(people_database,sql):
    sessions,admin=people_database
    await admin.execute(sql)
    with pytest.raises(PlatformToolError):
        await call(sessions)


@pytest.mark.asyncio
async def test_self_tool_cannot_impersonate_another_actor(people_database):
    with pytest.raises(PlatformToolError):
        await call(people_database[0],actor_user_id='alice')


@pytest.mark.asyncio
async def test_scope_errors_and_profile_fields_are_bounded(people_database):
    sessions,admin=people_database
    async with sessions.begin() as db:
        db.add(Scope(id='alice-personal',kind='personal',name='Private',owner_user_id='alice'))
    for scope, status in [('missing',404),('alice-personal',403)]:
        with pytest.raises(PlatformToolError) as exc:
            await call(sessions,'list_scope_users',{'scope_id':scope})
        assert exc.value.status_code==status
    await grant(sessions,descendants=True)
    result=await call(sessions,'list_scope_users',{'scope_id':'dept','include_descendants':False,'q':'ALICE@'})
    assert len(result['items'])==1
    assert set(result['items'][0])=={'id','display_name','email','departments'}
    await admin.execute("INSERT INTO scope_memberships(scope_id,user_id) VALUES ('dept','u')")
    assert (await call(sessions))['departments']==[{'id':'dept','name':'Department'}]


@pytest.mark.asyncio
async def test_management_group_deny_does_not_disable_profile_reads(people_database):
    sessions,admin=people_database
    await admin.execute("UPDATE agents SET tool_policy='{\"denied_groups\":[\"agent_management\",\"skill_management\"]}'")
    assert (await call(sessions))['id']=='u'
    with pytest.raises(PlatformToolError):
        await call(sessions,'platform_get_context')
