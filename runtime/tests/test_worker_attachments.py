"""Task input copies use real local files and a scoped, network-free backend."""
import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from workmate_runtime.assembly.attachments import materialize_start_attachments,AttachmentMaterializationError
from workmate_runtime.transport.postgres import ExecutionOwner


@dataclass(frozen=True)
class Message:
    id: str
    content: dict


class Backend:
    def __init__(self):
        self.writes = {}
    async def write_stream(self,path,stream):
        self.writes[path] = b''.join([chunk async for chunk in stream])


def inputs(tmp_path, *, name='report.txt',data=b'hello',size=None,path=None):
    source = tmp_path/'.inputs'/'conversation'/'task'
    source.mkdir(parents=True,exist_ok=True)
    (source/name).write_bytes(data)
    attachment = {'name':name,'path':path or f'input/task/{name}','mime_type':'text/plain',
        'size':len(data) if size is None else size}
    context = SimpleNamespace(conversation_id='conversation',task_id='task',session_id='session',
        input_messages=(Message('message',{'text':'Please analyze','attachments':[attachment]}),))
    backend = Backend()
    binding = SimpleNamespace(owner=ExecutionOwner('session','task','worker',1),backend=backend,
        workspace=SimpleNamespace(workdir='/workspace'))
    return context,binding,source


async def test_materializer_preserves_original_message_and_existing_platform_hint(tmp_path):
    context,binding,_ = inputs(tmp_path)
    result = await materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path)
    assert result[0].id == 'message'
    assert context.input_messages[0].content['text'] == 'Please analyze'
    assert binding.backend.writes == {'/workspace/input/task/report.txt':b'hello'}
    assert result[0].content['text'] == 'Please analyze\n\n[平台附件]\n以下文件属于本任务，已放入工作区：\n- input/task/report.txt\n请按需使用 Read 或其他工作区工具读取。'


async def test_plain_text_needs_no_attachment_storage_directory(tmp_path):
    context,binding,_ = inputs(tmp_path)
    context.input_messages = (Message('plain',{'text':'hello'}),)
    result = await materialize_start_attachments(context,binding,artifact_storage_dir=None)
    assert result == context.input_messages and not binding.backend.writes


@pytest.mark.parametrize('path', ['input/other/report.txt','../report.txt','/input/task/report.txt','input/task/../report.txt'])
async def test_cross_task_and_traversal_metadata_is_rejected_before_write(tmp_path,path):
    context,binding,_ = inputs(tmp_path,path=path)
    with pytest.raises(AttachmentMaterializationError):
        await materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path)
    assert not binding.backend.writes


@pytest.mark.parametrize('problem', ['missing','symlink','wrong-size','oversized','no-directory'])
async def test_unavailable_or_invalid_attachment_never_reports_success(tmp_path,problem):
    context,binding,source = inputs(tmp_path)
    if problem in {'missing','symlink'}:
        (source/'report.txt').unlink()
    if problem=='symlink':
        target = tmp_path/'foreign'
        target.write_bytes(b'hello')
        (source/'report.txt').symlink_to(target)
    if problem=='wrong-size':
        context.input_messages[0].content['attachments'][0]['size']=6
    if problem=='oversized':
        context.input_messages[0].content['attachments'][0]['size']=25*1024*1024+1
    with pytest.raises(AttachmentMaterializationError):
        await materialize_start_attachments(context,binding,
            artifact_storage_dir=None if problem=='no-directory' else tmp_path)
    assert not binding.backend.writes


@pytest.mark.parametrize('part', ['root','.inputs','conversation','task'])
async def test_each_directory_symlink_is_rejected(tmp_path,part):
    root = tmp_path/'root'
    context,binding,_ = inputs(root)
    path = root if part=='root' else root/'.inputs'
    if part in {'conversation','task'}:
        path /= 'conversation'
    if part=='task':
        path /= 'task'
    target = tmp_path/'renamed'
    path.rename(target)
    path.symlink_to(target,target_is_directory=True)
    with pytest.raises(AttachmentMaterializationError):
        await materialize_start_attachments(context,binding,artifact_storage_dir=root)
    assert not binding.backend.writes


async def test_all_sources_are_checked_before_any_destination_is_written(tmp_path):
    context,binding,_ = inputs(tmp_path)
    metadata = context.input_messages[0].content['attachments']
    metadata.append({**metadata[0],'name':'missing','path':'input/task/missing'})
    with pytest.raises(AttachmentMaterializationError):
        await materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path)
    assert not binding.backend.writes


@pytest.mark.parametrize('size', [True,-1,1.5,'5'])
async def test_size_is_strict_integer_metadata(tmp_path,size):
    context,binding,_ = inputs(tmp_path,size=size)
    with pytest.raises(AttachmentMaterializationError):
        await materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path)


async def test_ten_files_are_allowed_but_eleven_and_duplicate_names_are_rejected(tmp_path):
    context,binding,source = inputs(tmp_path)
    metadata = context.input_messages[0].content['attachments']
    for i in range(9):
        name=f'{i}.txt'
        (source/name).write_bytes(b'hello')
        metadata.append({**metadata[0],'name':name,'path':f'input/task/{name}'})
    await materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path)
    assert len(binding.backend.writes)==10
    metadata.append({**metadata[0],'name':'extra','path':'input/task/extra'})
    binding.backend.writes.clear()
    with pytest.raises(AttachmentMaterializationError):
        await materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path)
    assert not binding.backend.writes
    metadata[:] = [metadata[0],metadata[0]]
    with pytest.raises(AttachmentMaterializationError):
        await materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path)


async def test_exact_25_mib_and_empty_files_are_supported(tmp_path):
    context,binding,source = inputs(tmp_path,data=b'',size=25*1024*1024)
    with (source/'report.txt').open('wb') as file:
        file.truncate(25*1024*1024)
    counts=[]
    async def count(path,stream):
        counts.append(sum([len(chunk) async for chunk in stream]))
    binding.backend.write_stream=count
    await materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path)
    (source/'report.txt').write_bytes(b'')
    context.input_messages[0].content['attachments'][0]['size']=0
    await materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path)
    assert counts==[25*1024*1024,0]


async def test_cancelled_backend_transfer_does_not_return_augmented_messages(tmp_path):
    context,binding,_ = inputs(tmp_path)
    entered = asyncio.Event()
    async def blocked(path,stream):
        await anext(stream)
        entered.set()
        await asyncio.Event().wait()
    binding.backend.write_stream=blocked
    task=asyncio.create_task(materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert context.input_messages[0].content['text']=='Please analyze'


async def test_backend_must_consume_entire_stream_and_cannot_read_it_after_failure(tmp_path):
    context,binding,_ = inputs(tmp_path)
    captured=[]
    async def ignore(path,stream):
        captured.append(stream)
    binding.backend.write_stream=ignore
    with pytest.raises(AttachmentMaterializationError):
        await materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path)
    with pytest.raises(StopAsyncIteration):
        await anext(captured[0])


async def test_source_modification_during_copy_is_rejected(tmp_path):
    context,binding,source = inputs(tmp_path)
    async def modify(path,stream):
        await anext(stream)
        (source/'report.txt').write_bytes(b'longer')
        async for _ in stream:
            pass
    binding.backend.write_stream=modify
    with pytest.raises(AttachmentMaterializationError):
        await materialize_start_attachments(context,binding,artifact_storage_dir=tmp_path)


async def test_object_attachment_materializes_without_gateway_volume(tmp_path):
    from workmate_product.application.artifact_objects import ArtifactSnapshot
    context,binding,source = inputs(tmp_path)
    item = context.input_messages[0].content['attachments'][0]
    import hashlib
    item['sha256'] = hashlib.sha256(b'hello').hexdigest()
    item['storage_key'] = '.inputs/conversation/task/report.txt'
    calls = []
    class Store:
        def snapshot(self,key,digest,size):
            calls.append((key,digest,size))
            path = tmp_path/'private-snapshot'
            path.write_bytes(b'hello')
            return ArtifactSnapshot(path)
    await materialize_start_attachments(context,binding,artifact_storage_dir='/not-mounted',object_store=Store())
    assert binding.backend.writes == {'/workspace/input/task/report.txt': b'hello'}
    assert calls == [(item['storage_key'],item['sha256'],5)]
    assert not (tmp_path/'private-snapshot').exists()
    item['storage_key'] = '.inputs/another/task/report.txt'
    binding.backend.writes.clear()
    with pytest.raises(AttachmentMaterializationError):
        await materialize_start_attachments(context,binding,artifact_storage_dir=None,object_store=Store())
    assert not binding.backend.writes
