"""Upload acknowledgement is distinct from permission to start indexing."""
from unittest.mock import AsyncMock

import pytest
from app.adapter.errors import RuntimeError as RuntimeFailure
from app.config_file import KnowledgeFileConfig


async def setup_api(client_and_app,auth_headers,monkeypatch):
    http,app = client_and_app
    runtime = app.state.runtime_client
    documents = {}
    starts = []
    async def remote(method,owner,kb_id=None,*,suffix='',body=None):
        if method=='POST' and kb_id is None:
            return {'id':'runtime-kb'}
        if method=='GET' and suffix=='/documents':
            return {'items':[],'total':0}
        if method=='GET' and suffix.startswith('/staged/'):
            doc = documents.get(suffix.split('/')[-1])
            if doc is None:
                raise RuntimeFailure(404,'missing')
            return doc
        if method=='POST' and suffix.endswith('/start'):
            doc = documents.get(suffix.split('/')[-2])
            if doc is None:
                raise RuntimeFailure(404,'missing')
            if doc['status']=='uploaded':
                doc['status']='pending'
                starts.append(doc['id'])
            return doc
        if method=='DELETE' and suffix.startswith('/documents/'):
            documents.pop(suffix.split('/')[-1],None)
            return None
        raise AssertionError((method,suffix))
    async def upload(owner,kb,filename,mime,content,doc_id):
        documents[doc_id] = dict(id=doc_id,filename=filename,size=len(content),content_type=mime,
                                 status='uploaded',chunk_count=0,error=None)
        return documents[doc_id]
    runtime.knowledge_request = AsyncMock(side_effect=remote)
    runtime.knowledge_upload = AsyncMock(side_effect=upload)
    monkeypatch.setattr('app.services.knowledge.knowledge_settings',lambda:KnowledgeFileConfig(enabled=True))
    created = await http.post('/api/knowledge-bases',headers=auth_headers,json={'name':'Manual library'})
    assert created.status_code==201,created.text
    return http,runtime,documents,starts,'/api/knowledge-bases/'+created.json()['id']


async def test_upload_stays_uploaded_and_start_is_explicit(client_and_app,auth_headers,monkeypatch):
    http,runtime,docs,starts,url = await setup_api(client_and_app,auth_headers,monkeypatch)
    result = await http.post(url+'/documents',headers=auth_headers,files={'file':('manual.txt',b'manual')})
    assert result.status_code==202,result.text
    doc = result.json()
    assert doc['status']=='uploaded'
    assert not starts
    assert (await http.get(url+'/documents',headers=auth_headers)).json()['items'][0]['status']=='uploaded'
    for _ in range(2):
        result = await http.post(url+'/documents/'+doc['id']+'/start',headers=auth_headers)
        assert result.status_code==202,result.text
        assert result.json()['id']==doc['id'] and result.json()['status']=='pending'
    assert starts==[doc['id']]
    assert (await http.delete(url+'/documents/'+doc['id'],headers=auth_headers)).status_code==204
    assert (await http.get(url+'/documents',headers=auth_headers)).json()['total']==0


async def test_lost_upload_response_recovers_from_durable_identity(client_and_app,auth_headers,monkeypatch):
    http,runtime,docs,starts,url = await setup_api(client_and_app,auth_headers,monkeypatch)
    upload = runtime.knowledge_upload.side_effect
    async def response_lost(*args):
        await upload(*args)
        raise RuntimeFailure(503,'unknown outcome')
    runtime.knowledge_upload.side_effect=response_lost
    result = await http.post(url+'/documents',headers=auth_headers,files={'file':('manual.txt',b'manual')})
    assert result.status_code==503,result.text
    listed = (await http.get(url+'/documents',headers=auth_headers)).json()
    assert listed['total']==1 and listed['items'][0]['status']=='uploaded'
    assert not starts
    result = await http.post(url+'/documents/'+listed['items'][0]['id']+'/start',headers=auth_headers)
    assert result.status_code==202,result.text
    assert len(starts)==1


async def test_upload_failure_before_runtime_is_visible_and_deletable(client_and_app,auth_headers,monkeypatch):
    http,runtime,docs,starts,url = await setup_api(client_and_app,auth_headers,monkeypatch)
    runtime.knowledge_upload.side_effect=RuntimeFailure(503,'unavailable')
    assert (await http.post(url+'/documents',headers=auth_headers,files={'file':('a.txt',b'a')})).status_code==503
    listed=(await http.get(url+'/documents',headers=auth_headers)).json()
    assert listed['items'][0]['status']=='upload_failed'
    doc_id=listed['items'][0]['id']
    assert (await http.post(url+'/documents/'+doc_id+'/start',headers=auth_headers)).status_code==404
    assert (await http.delete(url+'/documents/'+doc_id,headers=auth_headers)).status_code==204
    assert not starts


async def test_start_checks_parent_library_authorization(client_and_app,auth_headers,monkeypatch):
    http,_,_,_,url = await setup_api(client_and_app,auth_headers,monkeypatch)
    registered=await http.post('/api/auth/register',json={'email':'manual-other@example.com','password':'password123','display_name':'Other'})
    other={'Authorization':'Bearer '+registered.json()['access_token']}
    assert (await http.post(url+'/documents/any/start',headers=other)).status_code==404


async def test_library_delete_processing_preflight_is_one_runtime_operation(client_and_app,auth_headers,monkeypatch):
    http,runtime,docs,starts,url=await setup_api(client_and_app,auth_headers,monkeypatch)
    uploaded=await http.post(url+'/documents',headers=auth_headers,files={'file':('a.txt',b'a')})
    doc_id=uploaded.json()['id']
    runtime.knowledge_request.reset_mock()
    runtime.knowledge_request.side_effect=RuntimeFailure(409,'indexing in progress')
    response=await http.delete(url,headers=auth_headers)
    assert response.status_code==409
    assert runtime.knowledge_request.await_count==1
    call=runtime.knowledge_request.call_args
    assert call.args[0]=='DELETE' and call.kwargs['body']=={'document_ids':[doc_id]}
    assert doc_id in docs
    # The Runtime may already be gone after a previous response/commit loss.
    runtime.knowledge_request.side_effect=RuntimeFailure(404,'already deleted')
    assert (await http.delete(url,headers=auth_headers)).status_code==204
    from app.db import get_sessionmaker
    from workmate_product.persistence.models import KnowledgeDocumentUpload
    async with get_sessionmaker()() as db:
        assert await db.get(KnowledgeDocumentUpload,doc_id) is None


async def test_manifest_before_blob_does_not_report_inflight_upload_failed(client_and_app,auth_headers,monkeypatch):
    http,runtime,docs,_,url=await setup_api(client_and_app,auth_headers,monkeypatch)
    from app.db import get_sessionmaker
    from workmate_product.persistence.models import KnowledgeDocumentUpload
    async with get_sessionmaker()() as db:
        row=KnowledgeDocumentUpload(id='inflight',knowledge_base_id=url.split('/')[-1],filename='a.txt',content_type='text/plain',size=1,state='uploading')
        db.add(row)
        await db.commit()
    docs['inflight']={'id':'inflight','status':'upload_failed','error':'partial'}
    item=(await http.get(url+'/documents',headers=auth_headers)).json()['items'][0]
    assert item['status']=='uploading' and item['error'] is None
    async with get_sessionmaker()() as db:
        row=await db.get(KnowledgeDocumentUpload,'inflight')
        row.state='upload_failed'
        await db.commit()
    item=(await http.get(url+'/documents',headers=auth_headers)).json()['items'][0]
    assert item['status']=='upload_failed'
