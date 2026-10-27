"""Tool text is normalized before both streaming and Core state persistence."""
import json

import pytest
from agentscope.message import TextBlock, ToolCallBlock, UserMsg
from agentscope.tool import ToolChunk, ToolResponse
from workmate_contracts import snapshot_digest
from test_core_engine import ConfirmTool, make_engine, collect
from workmate_runtime.engines.agentscope.tool_output import ToolOutputCharacters, normalize_tool_text
from test_postgres_execution_store import database, claim
from workmate_runtime.transport.postgres import ExecutionStore


@pytest.mark.parametrize(('raw', 'expected'), [
    ('', ''), ('中文😀\n\t\\u0000', '中文😀\n\t\\u0000'),
    ('a\x00b', 'a�b'), ('a\ud800b\udcff', 'a�b�'),
    ('\ud83d\ude00', '��'),
])
def test_normalization(raw, expected):
    assert normalize_tool_text(raw) == expected
    assert normalize_tool_text(expected) == expected


async def test_middleware_preserves_arguments_and_normalizes_final_response():
    call = ToolCallBlock(id='call', name='tool', input='{"value":"\\u0000"}')
    chunk = ToolChunk(content=[TextBlock(text='a\x00\ud800')])
    response = ToolResponse(content=[TextBlock(text='a\x00\ud800')])
    async def handler(**kwargs):
        assert kwargs['tool_call'] is call
        yield chunk
        yield response
    output = [item async for item in ToolOutputCharacters().on_acting(
        None, {'tool_call': call}, handler)]
    for original, result in zip([chunk, response], output):
        assert result.content[0].text == 'a��'
        assert result.id == original.id
        assert result.state == original.state
        assert original.content[0].text == 'a\x00\ud800'
    assert call.input == '{"value":"\\u0000"}'


async def core_character_result():
    class TextTool(ConfirmTool):
        async def check_permissions(self, tool_input, context):
            from agentscope.permission import PermissionBehavior, PermissionDecision
            return PermissionDecision(behavior=PermissionBehavior.ALLOW, message='test')

        async def __call__(self, **kwargs):
            self.calls += 1
            yield ToolChunk(content=[TextBlock(id='text', text='PDF\x00中文\ud800😀\ud83d')], is_last=False)
            yield ToolChunk(content=[TextBlock(id='text', text='\ude00')], is_last=True)
    tool = TextTool()
    engine = make_engine([
        [ToolCallBlock(id='call-1', name=tool.name, input='{}')],
        [TextBlock(text='done')],
    ], tool=tool, middlewares=[ToolOutputCharacters()])
    events = await collect(engine, UserMsg(name='user', content='read'))
    snapshot = engine.snapshot()
    values = [event.model_dump(mode='json') for event in events]
    assert ''.join(event.delta for event in events
                   if event.type == 'TOOL_RESULT_TEXT_DELTA') == 'PDF�中文�😀��'
    serialized = json.dumps(values, ensure_ascii=False)
    state_json = snapshot.model_dump_json()
    assert 'PDF�中文�😀' in serialized
    assert 'PDF�中文�😀��' in state_json
    assert '\\u0000' not in state_json
    assert tool.calls == 1
    from workmate_runtime.engines.agentscope import CoreSnapshot
    restored = CoreSnapshot.model_validate_json(state_json)
    assert snapshot_digest(restored.model_dump(mode='json')) == snapshot_digest(snapshot.model_dump(mode='json'))

    return snapshot, values


async def test_real_core_events_snapshot_and_restore_agree():
    await core_character_result()


async def test_real_core_character_output_persists_in_postgres(database):
    pool, admin = database
    snapshot, events = await core_character_result()
    owner = await claim(pool)
    async with pool.acquire() as connection:
        # Prove the underlying PostgreSQL constraint, not just Python JSON encoding.
        import asyncpg
        with pytest.raises(asyncpg.UntranslatableCharacterError):
            await connection.fetchval('SELECT $1::jsonb', json.dumps({'text': 'PDF\x00'}))
        store = ExecutionStore(connection)
        await store.save_boundary(owner, expected_state_version=0, status='completed',
            reply_id=snapshot.to_state().reply_id,
            snapshot=snapshot.model_dump(mode='json'), events=events)
    restored = json.loads(await admin.fetchval('SELECT snapshot FROM runtime_sessions'))
    assert snapshot_digest(restored) == snapshot_digest(snapshot.model_dump(mode='json'))
    saved = [json.loads(row['payload']) for row in await admin.fetch(
        'SELECT payload FROM runtime_events ORDER BY runtime_seq')]
    assert saved == events
