import pytest
from workmate_product.application.recovery_forks import choose_recovery_node, recovery_history, merge_manifest


def test_choose_healthy_other_node_without_namespace_transfer():
    assert choose_recovery_node(['old', 'new'], 'old') == 'new'
    assert choose_recovery_node(['old'],'old')=='old'
    with pytest.raises(ValueError):
        choose_recovery_node([], 'old')


def test_history_is_bounded_and_labels_untrusted_prior_text():
    text = recovery_history([('user', 'hello'), ('assistant', 'world')], 'retry')
    assert 'hello' in text and 'world' in text and text.endswith('retry')
    with pytest.raises(ValueError):
        recovery_history([('user', 'x' * 200001)], 'retry')


def test_manifest_preserves_checkpoint_and_adds_inputs_without_conflicts():
    entry = {'path':'working/a', 'storage_key':'.objects/sha256/'+'a'*2+'/'+ 'a'*64,'sha256':'a'*64,'size':1,'executable':False}
    result = merge_manifest({'version':1,'directories':['input','working','output'],'files':[entry],'total_bytes':1}, [])
    assert result['files'] == [entry]
    with pytest.raises(ValueError):
        merge_manifest(result, [{**entry, 'sha256':'b'*64}])

from workmate_product.application.workspace_checkpoint_manifest import validate_manifest

@pytest.mark.parametrize('path',['../secret','working/../secret','working//a','/working/a','working/a\\b'])
def test_manifest_rejects_escape(path):
    with pytest.raises(ValueError):
        validate_manifest({'version':1,'directories':['input','working','output'],'files':[
            {'path':path,'storage_key':'.objects/sha256/aa/'+'a'*64,'sha256':'a'*64,'size':1,'executable':False}], 'total_bytes':1})


def test_manifest_rejects_file_as_parent_and_bool_size():
    item={'path':'working/a','storage_key':'.objects/sha256/aa/'+'a'*64,'sha256':'a'*64,'size':1,'executable':False}
    manifest={'version':1,'directories':['input','working','output'],'files':[item,{**item,'path':'working/a/b'}],'total_bytes':2}
    with pytest.raises(ValueError): validate_manifest(manifest)
    with pytest.raises(ValueError): validate_manifest({**manifest,'files':[{**item,'size':True}],'total_bytes':1})


def test_verified_manifest_checks_existing_checkpoint_objects(tmp_path):
    from app.services.recovery_forks import _verified_manifest
    class Store:
        def snapshot(self,*args): raise RuntimeError('object missing')
    item={'path':'working/a','storage_key':'.objects/sha256/aa/'+'a'*64,'sha256':'a'*64,'size':1,'executable':False}
    manifest={'version':1,'directories':['input','working','output'],'files':[item],'total_bytes':1}
    with pytest.raises(RuntimeError,match='object missing'):
        _verified_manifest(Store(),manifest,[],[])


async def test_cancellation_joins_object_publisher_before_return():
    import asyncio
    import threading
    from app.services.recovery_forks import _joined_io
    entered,finish=threading.Event(),threading.Event()
    def publish():
        entered.set();finish.wait(5)
    task=asyncio.create_task(_joined_io(publish))
    async def started():
        while not entered.is_set():
            if task.done():
                await task
            await asyncio.sleep(.001)
    await asyncio.wait_for(started(),2)
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError): await task


def test_newer_delivered_artifact_is_retained_alongside_checkpoint(tmp_path):
    from app.services.recovery_forks import _verified_manifest
    from types import SimpleNamespace
    class Store:
        def snapshot(self,key,digest,size):
            path=tmp_path/digest;path.write_bytes(b'x')
            return SimpleNamespace(path=path,close=lambda:path.unlink())
    old={'path':'output/report.txt','storage_key':'.objects/sha256/aa/'+'a'*64,'sha256':'a'*64,'size':1,'executable':False}
    checkpoint={'version':1,'directories':['output'],'files':[old],'total_bytes':1}
    newer={'id':'artifact','name':'report.txt','storage_key':'.objects/sha256/bb/'+'b'*64,'sha256':'b'*64,'size':1}
    result=_verified_manifest(Store(),checkpoint,[],[newer])
    assert {f['path']:f['sha256'] for f in result['files']}=={
        'output/report.txt':'a'*64,'input/recovered-artifacts/artifact/report.txt':'b'*64}


def test_original_attachment_is_not_lost_to_modified_checkpoint(tmp_path):
    from app.services.recovery_forks import _verified_manifest
    from types import SimpleNamespace
    import hashlib
    original=b'original';digest=hashlib.sha256(original).hexdigest()
    class Store:
        def snapshot(self,key,expected,size):
            path=tmp_path/(expected or digest);path.write_bytes(original)
            return SimpleNamespace(path=path,close=lambda:path.unlink())
        def publish_file(self,key,source,expected,size):
            assert source.read()==original
    old={'path':'input/task/a.txt','storage_key':'.objects/sha256/aa/'+'a'*64,'sha256':'a'*64,'size':1,'executable':False}
    checkpoint={'version':1,'directories':['input','input/task'],'files':[old],'total_bytes':1}
    attachment={'task_id':'task','conversation_id':'conv','name':'a.txt','sha256':digest,'size':len(original)}
    result=_verified_manifest(Store(),checkpoint,[attachment],[])
    assert {f['path']:f['sha256'] for f in result['files']}=={
        'input/task/a.txt':'a'*64,'input/recovered-attachments/task/a.txt':digest}


def test_recovery_rejects_excessive_input_bytes_before_object_reads():
    from app.services.recovery_forks import _verified_manifest
    class Store:
        def snapshot(self,*args): pytest.fail('Oversized recovery input must be rejected before I/O')
    attachments=[{'conversation_id':'c','task_id':'t','name':str(i),'size':8} for i in range(4)]
    with pytest.raises(ValueError,match='input bytes'):
        _verified_manifest(Store(),None,attachments,[],{'max_files':10,'max_bytes':10,'max_file_bytes':10})
