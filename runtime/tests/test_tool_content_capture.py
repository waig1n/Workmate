import hashlib
import json
from copy import deepcopy

import pytest
from workmate_runtime.execution.tool_content import ToolContentCapture
from workmate_product.projections.translator import TranslationState, translate_event
from workmate_product.config.artifact_storage import ArtifactStoragePolicy, artifact_storage_overrides


class Store:
    def __init__(self):
        self.objects = {}
    def publish_file(self, key, source, digest, size):
        source.seek(0)
        content = source.read()
        assert len(content) == size
        assert hashlib.sha256(content).hexdigest() == digest
        self.objects[key] = content


def event(kind, **kwargs):
    return dict(type=kind, reply_id='reply', tool_call_id='call', **kwargs)


@pytest.mark.asyncio
async def test_large_arguments_exact_content_reference_and_bounded_projection():
    store = Store()
    capture = ToolContentCapture(store, inline_bytes=1024, preview_chars=128)
    source = json.dumps({'file_path': '/workspace/report.py', 'content': '中文☃' * 2000}, ensure_ascii=False)
    events = [event('TOOL_CALL_START', tool_call_name='Write')]
    events += [event('TOOL_CALL_DELTA', delta=char) for char in source]
    events += [event('TOOL_CALL_END')]
    original = deepcopy(events)
    output = await capture.transform(events)
    assert events == original
    assert len(output) == 4
    assert len(output[1]['delta']) == 128
    ref = output[2]['content_ref']
    assert store.objects[ref['storage_key']] == source.encode()
    assert ref['size_bytes'] == len(source.encode())
    assert not ref['incomplete']
    translated = translate_event(output[2], "conversation", TranslationState())
    assert translated[0]['name'] == 'TOOL_CONTENT_AVAILABLE'
    assert 'storage_key' not in str(translated)
    assert translated[0]['value']['sha256'] == ref['sha256']
    assert not capture.buffers


@pytest.mark.asyncio
async def test_small_results_and_parallel_calls_keep_bytes_and_terminal_order():
    capture = ToolContentCapture(Store())
    result = await capture.transform([
        event('TOOL_CALL_DELTA', delta='{"a":'),
        event('TOOL_RESULT_TEXT_DELTA', delta='hello'),
        event('TOOL_CALL_DELTA', delta='1}'),
        event('TOOL_CALL_END'), event('TOOL_RESULT_END', state='success')])
    assert [item['type'] for item in result] == ['TOOL_CALL_DELTA','TOOL_CALL_END','TOOL_RESULT_TEXT_DELTA','TOOL_RESULT_END']
    assert result[0]['delta'] == '{"a":1}'
    assert result[2]['delta'] == 'hello'


@pytest.mark.asyncio
async def test_stop_flushes_incomplete_and_permission_input_is_unchanged():
    store = Store()
    capture = ToolContentCapture(store, inline_bytes=16, preview_chars=8)
    approval = dict(type='REQUIRE_USER_CONFIRM', reply_id='reply', tool_calls=[{'arguments': {'content': 'x' * 500}}])
    result = await capture.transform([event('TOOL_CALL_DELTA', delta='x'*100), approval])
    assert result[-1] == approval
    assert result[1]['content_ref']['incomplete']
    assert list(store.objects.values()) == [b'x' * 100]
    assert await capture.flush() == []


@pytest.mark.asyncio
async def test_special_projection_calls_are_not_captured():
    capture = ToolContentCapture(Store(), inline_bytes=16)
    events = [event('TOOL_CALL_START', tool_call_name='search_knowledge'),
              event('TOOL_CALL_DELTA', delta='x'*100), event('TOOL_CALL_END'),
              event('TOOL_RESULT_TEXT_DELTA', delta='result'), event('TOOL_RESULT_END', state='success')]
    assert await capture.transform(events) == events
    assert not capture.buffers


@pytest.mark.asyncio
async def test_publication_failure_retains_buffer_and_never_emits_reference():
    class Broken(Store):
        def publish_file(self, *args):
            raise OSError('unavailable')
    capture = ToolContentCapture(Broken(), inline_bytes=16)
    with pytest.raises(OSError):
        await capture.transform([event('TOOL_CALL_DELTA', delta='x'*100), event('TOOL_CALL_END')])
    assert len(capture.buffers) == 1
    capture.close()
    assert not capture.buffers


def test_policy_defaults_overrides_and_invalid_values():
    assert not ArtifactStoragePolicy().tool_content_enabled
    assert ArtifactStoragePolicy(tool_content_enabled=True).backend == 'local'
    values = artifact_storage_overrides({'ARTIFACT_STORAGE_BACKEND':'s3',
        'ARTIFACT_STORAGE_TOOL_CONTENT_ENABLED':'true',
        'ARTIFACT_STORAGE_TOOL_CONTENT_INLINE_BYTES':'1024',
        'ARTIFACT_STORAGE_TOOL_CONTENT_PREVIEW_CHARS':'256'})
    policy = ArtifactStoragePolicy(**values)
    assert policy.tool_content_enabled and policy.tool_content_inline_bytes == 1024
    for name, value in [('tool_content_inline_bytes',0), ('tool_content_preview_chars',99999)]:
        with pytest.raises(ValueError):
            ArtifactStoragePolicy(**{name:value})


@pytest.mark.asyncio
async def test_large_write_diff_metadata_is_externalized_without_mutating_core():
    capture = ToolContentCapture(Store(), inline_bytes=1024, preview_chars=128)
    source = event('TOOL_RESULT_END', state='success', metadata={'diff': '+' + '中文' * 5000, 'file_path': '/workspace/report.py'})
    original = deepcopy(source)
    output = await capture.transform([source])
    assert source == original
    assert len(output) == 2
    assert output[-1]['state'] == 'success' and output[-1]['metadata'] == {'file_path': '/workspace/report.py'}
    descriptor = output[0]['content_ref']
    assert descriptor['kind'] == 'metadata'
    assert json.loads(capture.store.objects[descriptor['storage_key']]) == source['metadata']
    assert len(json.dumps(output)) < 3000


@pytest.mark.asyncio
async def test_json_escape_overhead_externalizes_before_event_size_limit():
    from workmate_contracts import MAX_PAYLOAD_BYTES
    store=Store()
    capture=ToolContentCapture(store,inline_bytes=131072,preview_chars=128)
    text='\x01'*65536
    output=await capture.transform([event('TOOL_CALL_DELTA',delta=text),event('TOOL_CALL_END')])
    assert len(output)==3 and len(store.objects)==1
    assert all(len(json.dumps(item).encode()) < MAX_PAYLOAD_BYTES for item in output)
    assert next(iter(store.objects.values()))==text.encode()


@pytest.mark.asyncio
async def test_metadata_json_escape_overhead_externalizes_before_event_limit():
    from workmate_contracts import MAX_PAYLOAD_BYTES
    store=Store()
    capture=ToolContentCapture(store,inline_bytes=131072,preview_chars=128)
    metadata={'diff': '汉'*43500, 'file_path': '/workspace/report.py'}
    assert len(json.dumps(metadata, ensure_ascii=False).encode()) < 131072
    source=event('TOOL_RESULT_END',state='success',metadata=metadata)
    assert len(json.dumps(source).encode()) > MAX_PAYLOAD_BYTES-4096
    output=await capture.transform([source])
    assert len(output)==2 and output[0]['type']=='TOOL_CONTENT_REFERENCE'
    assert output[0]['content_ref']['kind']=='metadata'
    assert json.loads(next(iter(store.objects.values())))==metadata
    assert all(len(json.dumps(item).encode()) < MAX_PAYLOAD_BYTES for item in output)
    assert output[-1]['metadata']=={'file_path':'/workspace/report.py'}
