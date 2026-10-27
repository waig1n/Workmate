"""Workspace lifecycle CAS on caller transactions; never starts or closes resources.

Lock order: Session -> residency -> release request. Redis health/affinity is not
an ownership source. A positive routing check does not grant execution authority.
"""
from dataclasses import dataclass
import hashlib
import json
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text


class ResidencyConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseClaim:
    request_id: str
    session_id: str
    worker_id: str
    node_id: str
    generation: str
    hold_version: int


class ReleaseReceipt(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    schema_version: int=Field(ge=1,le=1)
    session_id: str=Field(min_length=1,max_length=128)
    worker_id: str=Field(min_length=1,max_length=128)
    node_id: str=Field(min_length=1,max_length=128)
    workspace_generation: str=Field(min_length=1,max_length=128)
    hold_version: int=Field(ge=1)
    resources_quiescent: bool
    workspace_closed: bool
    ownership_released: bool
    task_id: str|None=None
    execution_epoch: int|None=Field(default=None,ge=1)


def _transaction(db):
    if not db.in_transaction() or db.bind.dialect.name!='postgresql':
        raise RuntimeError('Workspace residency requires a caller-owned PostgreSQL transaction')


async def _row(db,sql,**params):
    return (await db.execute(text(sql),params)).mappings().first()


async def _locked(db,session_id):
    _transaction(db)
    session=await _row(db,'SELECT * FROM runtime_sessions WHERE id=:id FOR UPDATE',id=session_id)
    resident=await _row(db,'SELECT * FROM workspace_residencies WHERE session_id=:id FOR UPDATE',id=session_id)
    if session is None:
        raise ResidencyConflict('Workspace Session is unavailable')
    return session,resident


async def read(db,session_id):
    _transaction(db)
    row=await _row(db,'SELECT * FROM workspace_residencies WHERE session_id=:id',id=session_id)
    return dict(row) if row else None


async def _owner(db,session,*,task_id,worker_id,execution_epoch,node_id):
    execution=await _row(db,'SELECT * FROM runtime_task_executions WHERE task_id=:id',id=task_id)
    if (not execution or session['superseded_at'] is not None or session['active_task_id']!=task_id or session['owner_id']!=worker_id
            or session['storage_node_id']!=node_id or session['execution_epoch']!=execution_epoch
            or execution['session_id']!=session['id'] or execution['execution_epoch']!=execution_epoch
            or not await db.scalar(text('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=:id'),{'id':session['id']})):
        raise ResidencyConflict('Workspace registration or release lacks the exact live execution owner')


async def record_held(db,*,session_id,task_id,worker_id,execution_epoch,node_id,generation):
    if type(execution_epoch) is not int or execution_epoch<1 or any(not isinstance(v,str) or not v.strip() or len(v)>128
            for v in (session_id,task_id,worker_id,node_id,generation)):
        raise ValueError('Complete bounded Workspace identities are required')
    session,resident=await _locked(db,session_id)
    await _owner(db,session,task_id=task_id,worker_id=worker_id,execution_epoch=execution_epoch,node_id=node_id)
    if resident is not None:
        if (resident['storage_node_id']!=node_id or resident['workspace_generation']!=generation
                or resident['status'] not in {'held','released'}
                or (resident['status']=='held' and resident['holder_worker_id']!=worker_id)):
            raise ResidencyConflict('Another or unresolved compute holding blocks registration')
        version=resident['hold_version']+(resident['status']=='released')
        await db.execute(text('''UPDATE workspace_residencies SET holder_worker_id=:worker,
            hold_version=:version,status='held',release_request_id=NULL,last_task_id=:task,
            last_execution_epoch=:epoch,updated_at=clock_timestamp() WHERE session_id=:id'''),
            {'id':session_id,'worker':worker_id,'version':version,'task':task_id,'epoch':execution_epoch})
    else:
        await db.execute(text('''INSERT INTO workspace_residencies(session_id,storage_node_id,workspace_generation,
            holder_worker_id,hold_version,status,last_task_id,last_execution_epoch)
            VALUES (:id,:node,:generation,:worker,1,'held',:task,:epoch)'''),
            {'id':session_id,'node':node_id,'generation':generation,'worker':worker_id,'task':task_id,'epoch':execution_epoch})
    await _owner(db,session,task_id=task_id,worker_id=worker_id,execution_epoch=execution_epoch,node_id=node_id)
    return await read(db,session_id)


async def start_allowed(db,session_id,worker_id,node_id):
    session,resident=await _locked(db,session_id)
    return (session['superseded_at'] is None and session['storage_node_id']==node_id and (resident is None or (
        resident['storage_node_id']==node_id and (resident['status']=='released' or (
            resident['status']=='held' and resident['holder_worker_id']==worker_id)))))


def _request_id(session_id,version):
    return 'release-'+hashlib.sha256(f'{session_id}:{version}'.encode()).hexdigest()


def _same(resident,claim):
    return resident is not None and all(resident[key]==value for key,value in {
        'session_id':claim.session_id,'holder_worker_id':claim.worker_id,'storage_node_id':claim.node_id,
        'workspace_generation':claim.generation,'hold_version':claim.hold_version}.items())


def _claim(request):
    return ReleaseClaim(request['id'],request['session_id'],request['expected_worker_id'],
        request['storage_node_id'],request['workspace_generation'],request['expected_hold_version'])


async def _insert_request(db,resident,reason):
    request_id=_request_id(resident['session_id'],resident['hold_version'])
    await db.execute(text('''INSERT INTO workspace_release_requests(id,session_id,storage_node_id,
        expected_worker_id,expected_hold_version,workspace_generation,reason)
        VALUES (:id,:sid,:node,:worker,:version,:generation,:reason) ON CONFLICT DO NOTHING'''),
        {'id':request_id,'sid':resident['session_id'],'node':resident['storage_node_id'],
         'worker':resident['holder_worker_id'],'version':resident['hold_version'],
         'generation':resident['workspace_generation'],'reason':reason})
    return await _row(db,'SELECT * FROM workspace_release_requests WHERE id=:id FOR UPDATE',id=request_id)


async def request_release(db,*,session_id,expected_worker,expected_hold_version,generation,node_id):
    _,resident=await _locked(db,session_id)
    claim=ReleaseClaim(_request_id(session_id,expected_hold_version),session_id,expected_worker,node_id,generation,expected_hold_version)
    if not _same(resident,claim):
        return None
    return dict(await _insert_request(db,resident,'idle'))


async def _quiet(db,session):
    if session['active_task_id'] is not None or session['owner_id'] is not None or session['lease_until'] is not None:
        return False
    if await db.scalar(text("SELECT 1 FROM runtime_commands WHERE session_id=:id AND status IN ('claimed','indeterminate') LIMIT 1"),{'id':session['id']}):
        return False
    # A new admitted but not claimed Start may wait for this release. Terminal
    # delivery retaining the old execution gate was rejected above.
    pending=await _row(db,'''SELECT c.active_product_task_id,t.status,e.status AS execution_status,e.execution_epoch FROM conversations c
        LEFT JOIN tasks t ON t.id=c.active_product_task_id
        LEFT JOIN runtime_task_executions e ON e.task_id=t.id WHERE c.id=:id''',id=session['conversation_id'])
    return bool(pending is not None and (pending['active_product_task_id'] is None or (
        pending['status'] in {'created','cancelling'} and pending['execution_status']=='accepted'
        and pending['execution_epoch']==0)))


async def _lock_request(db,request_id):
    candidate=await _row(db,'SELECT session_id FROM workspace_release_requests WHERE id=:id',id=request_id)
    if not candidate:
        raise ResidencyConflict('Workspace release request is unavailable')
    session,resident=await _locked(db,candidate['session_id'])
    request=await _row(db,'SELECT * FROM workspace_release_requests WHERE id=:id FOR UPDATE',id=request_id)
    return session,resident,request


async def begin_release(db,request_id,worker_id,node_id):
    session,resident,request=await _lock_request(db,request_id)
    claim=_claim(request)
    if claim.worker_id!=worker_id or claim.node_id!=node_id or request['status']!='pending':
        return None
    if not _same(resident,claim):
        await db.execute(text("UPDATE workspace_release_requests SET status='stale',completed_at=clock_timestamp() WHERE id=:id"),{'id':request_id})
        return None
    if resident['status']!='held' or not await _quiet(db,session):
        return None
    await db.execute(text("UPDATE workspace_residencies SET status='releasing',release_request_id=:request,updated_at=clock_timestamp() WHERE session_id=:id"),{'id':claim.session_id,'request':request_id})
    await db.execute(text("UPDATE workspace_release_requests SET status='releasing' WHERE id=:id"),{'id':request_id})
    return claim


async def confirm_release_claim(db,claim):
    """Confirm an already returned claim after an ambiguous begin commit.

    This read-only confirmation neither elects a cleanup winner nor retries
    resource closure. The caller must retain the exact claim it received inside
    the original transaction; a request envelope is not a cleanup capability.
    """
    session,resident=await _locked(db,claim.session_id)
    request=await _row(db,'SELECT * FROM workspace_release_requests WHERE id=:id FOR UPDATE',
        id=claim.request_id)
    return bool(request is not None and _claim(request)==claim
        and request['status']=='releasing' and _same(resident,claim)
        and resident['status']=='releasing' and resident['release_request_id']==claim.request_id
        and await _quiet(db,session))


def _proof(claim,receipt,*,task_id=None,execution_epoch=None):
    proof=ReleaseReceipt.model_validate(receipt)
    if (not proof.resources_quiescent or not proof.workspace_closed or not proof.ownership_released
            or any(getattr(proof,k)!=value for k,value in {
                'session_id':claim.session_id,'worker_id':claim.worker_id,'node_id':claim.node_id,
                'workspace_generation':claim.generation,'hold_version':claim.hold_version,
                'task_id':task_id,'execution_epoch':execution_epoch}.items())):
        raise ValueError('Release receipt must prove closure of this exact holding')
    return proof.model_dump(mode='json')


def _retry(request,payload):
    if request['status']!='released':
        return False
    stored=request['cleanup_receipt']
    if isinstance(stored,str): stored=json.loads(stored)
    if stored!=payload:
        raise ResidencyConflict('Committed release has different evidence')
    return True


async def _finish(db,claim,payload):
    await db.execute(text("""UPDATE workspace_release_requests SET status='released',cleanup_receipt=CAST(:proof AS jsonb),
        completed_at=clock_timestamp(),error_code=NULL WHERE id=:id"""),{'id':claim.request_id,'proof':json.dumps(payload)})
    await db.execute(text("""UPDATE workspace_residencies SET status='released',release_request_id=:request,
        updated_at=clock_timestamp() WHERE session_id=:id"""),{'id':claim.session_id,'request':claim.request_id})


async def finish_release(db,claim,receipt):
    payload=_proof(claim,receipt)
    session,resident,request=await _lock_request(db,claim.request_id)
    if _claim(request)!=claim:
        raise ResidencyConflict('Release identity changed')
    if _retry(request,payload):
        return
    if not _same(resident,claim) or resident['status']!='releasing' or request['status']!='releasing' or not await _quiet(db,session):
        raise ResidencyConflict('Workspace release lost its idle exclusion')
    await _finish(db,claim,payload)


async def finish_owned_release(db,*,session_id,task_id,worker_id,execution_epoch,node_id,generation,hold_version,receipt):
    """Trusted execution cleanup closes compute but retains Product execution gates."""
    session,resident=await _locked(db,session_id)
    claim=ReleaseClaim(_request_id(session_id,hold_version),session_id,worker_id,node_id,generation,hold_version)
    payload=_proof(claim,receipt,task_id=task_id,execution_epoch=execution_epoch)
    previous=await _row(db,'SELECT * FROM workspace_release_requests WHERE id=:id FOR UPDATE',id=claim.request_id)
    if previous and _claim(previous)==claim and _retry(previous,payload):
        return
    await _owner(db,session,task_id=task_id,worker_id=worker_id,execution_epoch=execution_epoch,node_id=node_id)
    if (not _same(resident,claim) or resident['status']!='held'
            or resident['last_task_id']!=task_id or resident['last_execution_epoch']!=execution_epoch):
        raise ResidencyConflict('Execution cleanup does not own the current compute holding')
    request=await _insert_request(db,resident,'execution_cleanup')
    if request['status'] not in {'pending','releasing'}:
        raise ResidencyConflict('Execution cleanup release has an unresolved prior disposition')
    await db.execute(text("UPDATE workspace_release_requests SET reason='execution_cleanup' WHERE id=:id"),{'id':claim.request_id})
    await _finish(db,claim,payload)
    await _owner(db,session,task_id=task_id,worker_id=worker_id,execution_epoch=execution_epoch,node_id=node_id)


async def block_release(db,claim,error_code):
    if not isinstance(error_code,str) or not error_code or len(error_code)>128:
        raise ValueError('A bounded error category is required')
    _,resident,request=await _lock_request(db,claim.request_id)
    if _claim(request)!=claim or not _same(resident,claim) or resident['status']!='releasing' or request['status']!='releasing':
        raise ResidencyConflict('Failed release no longer owns its holding')
    await db.execute(text("UPDATE workspace_release_requests SET status='blocked',error_code=:error WHERE id=:id"),{'id':claim.request_id,'error':error_code})
    await db.execute(text("UPDATE workspace_residencies SET status='blocked',updated_at=clock_timestamp() WHERE session_id=:id"),{'id':claim.session_id})


async def pending_requests(db,worker_id,node_id,*,limit=32):
    _transaction(db)
    if type(limit) is not int or not 1<=limit<=1000:
        raise ValueError('A bounded release batch is required')
    return [dict(row) for row in (await db.execute(text("""SELECT * FROM workspace_release_requests
        WHERE expected_worker_id=:worker AND storage_node_id=:node AND status='pending'
        ORDER BY created_at,id LIMIT :limit"""),{'worker':worker_id,'node':node_id,'limit':limit})).mappings()]
