"""Product delivery projection and admission release in one caller transaction.

Only immutable captured descriptors are consumed. No Workspace reads, tool calls,
file copies, independent commits, or new Task dispatch happen under these locks.
"""
import mimetypes
from pathlib import PurePosixPath
import uuid

from workmate_product.persistence.repositories import delivery as delivery_store

from workmate_product.persistence.models import Artifact, TaskArtifactDelivery
from workmate_product.projections.store import ProjectedEvent,ProjectionConflict,append_business_events,lock_product_conversation
from .delivery_metadata import DeliveryConflict,_digest,_validate


async def close_delivery(db,conversation_id,task_id):
    """Return stable Artifact IDs. An unknown/unfinished boundary never releases.

    Retrying a completed delivery reads its receipt even after a following Task
    acquired the gates. Callers must not expose this internal operation directly
    as an unauthenticated browser API.
    """
    if not db.in_transaction() or db.bind.dialect.name!='postgresql':
        raise DeliveryConflict('Delivery closure needs a caller-owned PostgreSQL transaction')
    async with db.begin_nested():
        try:
            conv=await lock_product_conversation(db,conversation_id)
        except ProjectionConflict as error:
            raise DeliveryConflict('Delivery Conversation identity is unavailable') from error
        task=await delivery_store.lock_task(db, id=task_id)
        capture=await delivery_store.read_capture(db, id=task_id)
        projection=await delivery_store.lock_projection(db, id=task_id)
        if (capture is None and projection and projection['state'].get('terminal_control_event_id')):
            from .delivery_unstarted import close_unstarted_delivery
            return await close_unstarted_delivery(db,conv,task,projection)
        if (not conv or not task or task['conversation_id']!=conversation_id or not capture or not projection
                or projection['format_version']!=1 or projection['session_id']!=capture['session_id']):
            raise DeliveryConflict('Delivery projection lacks its immutable source')
        payload=capture['payload']
        _validate(payload,capture=True)
        if _digest(payload)!=capture['digest']:
            raise DeliveryConflict('Captured delivery manifest digest changed')
        state=projection['state']
        if state.get('delivery_closed'):
            receipt=state['delivery_closed']
            if receipt.get('digest')!=capture['digest']:
                raise DeliveryConflict('Delivery receipt differs from captured source')
            return tuple(receipt['artifact_ids'])
        session=await delivery_store.lock_session(db, id=capture['session_id'])
        execution=await delivery_store.lock_execution(db, id=task_id)
        snapshot=await delivery_store.read_snapshot(db, id=capture['snapshot_id'])
        terminal={'completed','failed','cancelled','interrupted'}
        if (not session or not execution or not snapshot or task['status'] not in terminal
                or task['run_id']!=snapshot['reply_id'] or conv['active_product_task_id']!=task_id
                or session['active_task_id']!=task_id or session['conversation_id']!=conversation_id
                or execution['session_id']!=session['id'] or snapshot['task_id']!=task_id
                or snapshot['session_id']!=session['id'] or session['status'] not in terminal
                or session['snapshot_id']!=capture['snapshot_id'] or execution['snapshot_id']!=capture['snapshot_id']
                or session['owner_id']!=capture['worker_id'] or session['storage_node_id']!=capture['storage_node_id']
                or session['execution_epoch']!=capture['execution_epoch']
                or any(execution[k]!=session[k] or snapshot[k]!=session[k] for k in
                    ('execution_epoch','state_version','reply_id','status'))
                or snapshot['runtime_seq']!=session['runtime_seq']
                or state.get('terminal_boundary_id')!=snapshot['id']
                or projection['last_runtime_seq']<snapshot['runtime_seq']):
            raise DeliveryConflict('Delivery boundary is not fully projected under the active gates')
        if await delivery_store.has_unsettled_commands(db, id=task_id):
            raise DeliveryConflict('Control or execution commands have not settled')
        await delivery_store.append_delivery_audit(db,conversation_id,task_id,'artifact_delivery',{'source':payload['source'],
            'declaredFiles':payload['declared_files'],'warnings':payload['warnings']})
        ids=[]
        for index,file in enumerate(payload['files']):
            suffix=PurePosixPath(file['name']).suffix.lower()
            mime={'.md':'text/markdown','.yaml':'application/yaml','.yml':'application/yaml'}.get(
                suffix,mimetypes.guess_type(file['name'])[0] or 'application/octet-stream')
            previous=await delivery_store.latest_artifact(db, cid=conversation_id, name=file['name'])
            unchanged=bool(previous and previous.size==file['size']
                and (previous.metadata_ or {}).get('sha256')==file['sha256'])
            change='presented' if unchanged else 'modified' if previous else 'created'
            metadata={'runId':task['run_id'],'workspacePath':'output/'+file['name'],'sha256':file['sha256'],
                'deliverySource':payload['source'],'declared':payload['source']=='explicit'}
            if unchanged:
                artifact=previous
            else:
                artifact=Artifact(id=str(uuid.uuid4()),task_id=task_id,conversation_id=conversation_id,
                    name=file['name'],mime_type=mime,storage_key=file['storage_key'],size=file['size'],
                    version=previous.version+1 if previous else 1,change_type=change,metadata_=metadata)
                await delivery_store.insert_artifact(db, artifact)
            delivery=TaskArtifactDelivery(task_id=task_id,artifact_id=artifact.id,
                change_type=change,metadata_=metadata)
            await delivery_store.insert_delivery(db, delivery)
            ids.append(artifact.id)
            await delivery_store.append_delivery_audit(db,conversation_id,task_id,'artifact',{'artifactId':artifact.id,'name':artifact.name,
                'changeType':change,'size':artifact.size})
            value={'artifactId':artifact.id,'taskId':task_id,'name':artifact.name,'mimeType':artifact.mime_type,
                'size':artifact.size,'version':artifact.version,'changeType':change,
                'createdAt':delivery.delivered_at.isoformat()}
            await append_business_events(db,conversation_id,operation_key=f'delivery:{task_id}:{index}',
                task_id=task_id,reply_id=task['run_id'],items=[ProjectedEvent(key='artifact',event_type='CUSTOM',
                payload={'type':'CUSTOM','name':'ARTIFACT_CREATED','value':value})])
        updated={**state,'delivery_closed':{'digest':capture['digest'],'artifact_ids':ids}}
        await delivery_store.record_closure(db, task_id=task_id, conversation_id=conversation_id,
            session_id=session['id'], state=updated)
        return tuple(ids)
