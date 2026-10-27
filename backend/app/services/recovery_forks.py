"""Manual recovery admission; S3 reads precede the short admission transaction."""
import asyncio
import hashlib
import json
import uuid
from copy import deepcopy
from fastapi import HTTPException
from workmate_product.application.execution_quarantine import is_quarantined
from workmate_product.application.recovery_forks import WARNINGS, choose_recovery_node, recovery_history, merge_manifest
from workmate_product.artifact_storage import artifact_store_from_settings
from workmate_product.persistence.models import Conversation
from workmate_product.persistence.recovery_forks import RecoveryFork
from workmate_product.persistence.repositories import recovery_forks as repo
from ..config import get_settings


async def _joined_io(function, *args):
    operation=asyncio.create_task(asyncio.to_thread(function,*args))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                pass
            except Exception:
                break
        if operation.done() and not operation.cancelled():
            operation.exception()
        raise


async def _base_manifest(db, conversation_id, checkpoint):
    if checkpoint is not None:
        digest=hashlib.sha256(json.dumps(checkpoint.manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if digest!=checkpoint.digest:
            raise HTTPException(409,'文件检查点校验失败')
        return deepcopy(checkpoint.manifest)
    fork=await repo.fork_for(db,conversation_id)
    if fork is None:
        return None
    from workmate_contracts import snapshot_digest
    expected=await repo.fork_manifest_digest(db,fork.target_task_id)
    if snapshot_digest(fork.manifest)!=expected:
        raise HTTPException(409,'原恢复文件清单校验失败')
    return deepcopy(fork.manifest)


async def _source(db, user_id, conversation_id, *, lock=False):
    source = await repo.source(db,conversation_id,user_id,lock=lock)
    if source is None:
        raise HTTPException(404,'Conversation not found')
    conv,task,session = source
    if task is None or session is None or not is_quarantined(task.error) or task.status!='failed':
        raise HTTPException(409,'仅支持已隔离的故障任务恢复')
    return source


async def preview(db, user_id, conversation_id, admission):
    source = await repo.source(db,conversation_id,user_id)
    if source is None:
        raise HTTPException(404,'Conversation not found')
    fork = await repo.fork_for(db,conversation_id)
    base = {'eligible':False,'file_count':0,'checkpoint_created_at':None,'warnings':WARNINGS,
            'source_conversation_id':fork.source_conversation_id if fork else None,'memory_disabled':fork is not None,
            'has_file_snapshot':False}
    conv,task,session=source
    if task is None or session is None or task.status!='failed' or not is_quarantined(task.error):
        return {**base,'reason':None}
    if not await admission._authorize(db,user_id,conv.agent_id):
        return {**base,'reason':'当前智能体或能力访问不可用'}
    try:
        choose_recovery_node(admission.node_directory.candidates() if admission.node_directory else [],session.storage_node_id)
    except ValueError as error:
        return {**base,'reason':str(error)}
    if not get_settings().artifact_storage.workspace_checkpoint_enabled:
        return {**base,'reason':'尚未启用文件检查点恢复'}
    if artifact_store_from_settings(get_settings()) is None:
        return {**base,'reason':'恢复需要共享对象存储'}
    if await repo.has_queued(db,conv.id):
        return {**base,'reason':'请先取消该对话中尚未执行的排队任务，再恢复失败任务'}
    checkpoint=await repo.checkpoint_for(db,conv.id,task)
    messages=await repo.messages_for(db,conv.id,task)
    artifacts=await repo.artifacts_for(db,conv.id,task)
    base_manifest=await _base_manifest(db,conv.id,checkpoint)
    paths={f['path'] for f in base_manifest['files']} if base_manifest else set()
    names=set()
    for a in reversed(artifacts):
        if a.name not in names:
            names.add(a.name)
            path='output/'+a.name
            paths.add(f'input/recovered-artifacts/{a.id}/{a.name}' if path in paths else path)
    checkpoint_files={f['path']:f for f in (base_manifest or {}).get('files',[])}
    for m in messages:
        if m.role!='user':
            continue
        for a in m.content.get('attachments',[]):
            path=f'input/{m.task_id}/{a["name"]}'
            old=checkpoint_files.get(path)
            if old and (old['sha256']!=a.get('sha256') or old['size']!=a['size']):
                path=f'input/recovered-attachments/{m.task_id}/{a["name"]}'
            paths.add(path)
    warnings=list(WARNINGS)
    if checkpoint is None:
        warnings.append('沿用上次恢复的文件清单，之后未保存的修改不可恢复。' if base_manifest else
            '没有可用工作文件检查点，仅能恢复已保存的附件和成果，工作目录中的其他文件不可恢复。')
    return {**base,'eligible':True,'file_count':len(paths),'warnings':warnings,'has_file_snapshot':base_manifest is not None,
        'checkpoint_created_at':checkpoint.created_at.isoformat() if checkpoint else None}


def _verified_manifest(store, checkpoint, attachments, artifacts, limits=None):
    manifest=deepcopy(checkpoint)
    limits=limits or {'max_files':1000,'max_bytes':134217728,'max_file_bytes':16777216}
    from workmate_product.application.workspace_checkpoint_manifest import validate_manifest
    if manifest is not None:
        validate_manifest(manifest,**limits)
    if len(attachments)+len(artifacts)+len((manifest or {}).get('files',[]))>limits['max_files']*3:
        raise ValueError('Recovery input inventory exceeds limit')
    # Each source set may overlap the other two. Bound pre-deduplication I/O
    # as well as the final manifest; do not upload an unbounded history first.
    source_entries = [*(manifest or {}).get('files', []), *attachments, *artifacts]
    if any(type(item.get('size')) is not int or not 0 <= item['size'] <= limits['max_file_bytes']
           for item in source_entries):
        raise ValueError('Recovery input bytes are invalid')
    if sum(item['size'] for item in source_entries) > limits['max_bytes'] * 3:
        raise ValueError('Recovery input bytes exceed verification budget')
    additions=[]
    existing={f['path'] for f in (manifest or {}).get('files',[])}
    checkpoint_files={f['path']:f for f in (manifest or {}).get('files',[])}
    for item in attachments:
        if type(item.get('size')) is not int or not 0<=item['size']<=limits['max_file_bytes']:
            raise ValueError('Attachment exceeds recovery bound')
        path=f'input/{item["task_id"]}/{item["name"]}'
        key=f'.inputs/{item["conversation_id"]}/{item["task_id"]}/{item["name"]}'
        if item.get('storage_key',key)!=key:
            raise ValueError('Attachment identity differs from original task')
        snapshot=store.snapshot(key,item.get('sha256'),item['size'])
        try:
            with snapshot.path.open('rb') as source:
                digest=hashlib.file_digest(source,'sha256').hexdigest()
                target=f'.objects/sha256/{digest[:2]}/{digest}'
                source.seek(0)
                store.publish_file(target,source,digest,item['size'])
        finally:
            snapshot.close()
        previous=checkpoint_files.get(path)
        if previous is not None:
            if previous['sha256']==digest and previous['size']==item['size']:
                continue
            path=f'input/recovered-attachments/{item["task_id"]}/{item["name"]}'
        additions.append({'path':path,'storage_key':target,'sha256':digest,'size':item['size'],'executable':False})
        existing.add(path)
    # Newest delivered version wins; a checkpoint remains authoritative for its paths.
    artifact_names=set()
    for artifact in reversed(artifacts):
        if artifact['name'] in artifact_names:
            continue
        artifact_names.add(artifact['name'])
        path='output/'+artifact['name']
        if path in existing:
            path=f'input/recovered-artifacts/{artifact["id"]}/{artifact["name"]}'
        additions.append({'path':path,'storage_key':artifact['storage_key'],'sha256':artifact['sha256'],
            'size':artifact['size'],'executable':False})
        existing.add(path)
    result=merge_manifest(manifest,additions)
    # Shared pure validation lives in Product; Worker validates again before restore.
    from workmate_product.application.workspace_checkpoint_manifest import validate_manifest
    validate_manifest(result, **(limits or {}))
    for item in result['files']:
        snapshot=store.snapshot(item['storage_key'],item['sha256'],item['size'])
        snapshot.close()
    return result


async def recover(db,user,conversation_id,request_id,admission):
    from sqlalchemy import text
    from workmate_product.persistence.repositories.conversation_purge import ARTIFACT_STORAGE_LOCK
    async with admission.sessions.begin() as publication:
        await publication.execute(text('SELECT pg_advisory_xact_lock_shared(:key)'), {'key':ARTIFACT_STORAGE_LOCK})
        return await _recover(db,user,conversation_id,request_id,admission)


async def _recover(db,user,conversation_id,request_id,admission):
    actor_id=user.id
    existing=await repo.request_for(db,actor_id,request_id)
    if existing:
        if existing.source_conversation_id!=conversation_id:
            raise HTTPException(409,'Recovery request identity already used')
        return {'conversation_id':existing.conversation_id,'task_id':existing.target_task_id}
    conv,task,session=await _source(db,actor_id,conversation_id)
    if not await admission._authorize(db,actor_id,conv.agent_id):
        raise HTTPException(403,'Current Agent access is denied')
    try:
        node=choose_recovery_node(admission.node_directory.candidates() if admission.node_directory else [],session.storage_node_id)
    except ValueError as error:
        raise HTTPException(503,str(error)) from error
    if not get_settings().artifact_storage.workspace_checkpoint_enabled:
        raise HTTPException(409,'尚未启用文件检查点恢复')
    store=artifact_store_from_settings(get_settings())
    if store is None:
        raise HTTPException(409,'恢复需要共享对象存储')
    if await repo.has_queued(db,conv.id):
        raise HTTPException(409,'请先取消该对话中尚未执行的排队任务，再恢复失败任务')
    checkpoint=await repo.checkpoint_for(db,conv.id,task)
    manifest=await _base_manifest(db,conv.id,checkpoint)
    messages=await repo.messages_for(db,conv.id,task)
    artifacts=await repo.artifacts_for(db,conv.id,task)
    original=[m for m in messages if m.task_id==task.id and m.role=='user']
    if not original:
        raise HTTPException(409,'原任务输入缺失')
    request_text='\n'.join(m.content.get('text','') for m in original)
    text=recovery_history([(m.role,m.content.get('text','')) for m in messages if m.task_id!=task.id],request_text)
    attachments=[{**a,'task_id':m.task_id,'conversation_id':conv.id}
        for m in messages if m.role=='user' for a in m.content.get('attachments',[])]
    artifact_inputs=[{'id':a.id,'name':a.name,'storage_key':a.storage_key,'size':a.size,'sha256':(a.metadata_ or {}).get('sha256')} for a in artifacts]
    checkpoint_id=checkpoint.id if checkpoint else None
    source_task_id,source_session_id,agent_id=task.id,session.id,conv.agent_id
    approval_profile=task.approval_profile if task.approval_profile not in {'full_access','unattended'} else 'standard'
    await db.rollback()  # No open database transaction during object I/O.
    try:
        policy=get_settings().artifact_storage
        limits={'max_files':policy.workspace_checkpoint_max_files,'max_bytes':policy.workspace_checkpoint_max_bytes,
            'max_file_bytes':policy.workspace_checkpoint_max_file_bytes}
        manifest=await _joined_io(_verified_manifest,store,manifest,attachments,artifact_inputs,limits)
    except Exception as error:
        raise HTTPException(409,'恢复文件缺失或校验失败，未启动任务') from error
    paths='\n'.join(item['path'] for item in manifest['files'])
    text+='\n\n[平台恢复文件说明]\n已恢复最近检查点与保存文件；input/recovered-artifacts/ 下保存独立交付版本，input/recovered-attachments/ 下保存未经工具修改的原始上传附件。\n'+paths[:16000]
    try:
        await repo.lock_request(db,actor_id,request_id)
        existing=await repo.request_for(db,actor_id,request_id)
        if existing:
            if existing.source_conversation_id!=conversation_id:
                raise HTTPException(409,'Recovery request identity already used')
            return {'conversation_id':existing.conversation_id,'task_id':existing.target_task_id}
        from workmate_product.persistence.repositories.task_capacity import lock_capacity
        await lock_capacity(db)
        await admission._lock_conversation(db,conversation_id)
        current,failed,old_session=await _source(db,actor_id,conversation_id,lock=True)
        if failed.id!=source_task_id or old_session.id!=source_session_id or current.agent_id!=agent_id:
            raise HTTPException(409,'原任务状态已改变')
        if await repo.has_queued(db,conversation_id):
            raise HTTPException(409,'请先取消尚未执行的排队任务')
        new_session_id,new_task_id=str(uuid.uuid4()),str(uuid.uuid4())
        await repo.replace_current(db,current,old_session,new_session_id,target_node=node)
        db.add(RecoveryFork(id=str(uuid.uuid4()),conversation_id=conversation_id,source_conversation_id=conversation_id,
            source_task_id=source_task_id,source_session_id=source_session_id,target_session_id=new_session_id,
            target_task_id=new_task_id,target_node_id=node,actor_user_id=actor_id,context_text=text,
            request_key=request_id,checkpoint_id=checkpoint_id,manifest=manifest))
        await db.flush()
        # Existing entry owns permission, shared capacity, command and transaction commit.
        from workmate_product.persistence.models import User
        fresh_user=await db.get(User,actor_id)
        await admission.create_task(db,fresh_user,current,request_text,task_id=new_task_id,approval_profile=approval_profile)
        return {'conversation_id':conversation_id,'task_id':new_task_id}
    except BaseException:
        await db.rollback()
        raise
