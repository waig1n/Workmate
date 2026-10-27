import json
import uuid

from sqlalchemy import select
from workmate_product.persistence.models import Task, TaskEvent

from workmate_product.projections.translator import TranslationState, translate_event


def test_retrieval_deltas_never_enter_browser_or_result_buffer():
    state = TranslationState()
    def send(kind, **fields):
        return translate_event({"type": kind, "reply_id": "r", **fields}, "c", state)
    send("REPLY_START")
    frames = []
    for index in range(20):
        tool_id = str(index)
        frames += send("TOOL_CALL_START", tool_call_id=tool_id, tool_call_name="search_knowledge")
        for kind in ("TOOL_CALL_DELTA", "TOOL_RESULT_TEXT_DELTA"):
            assert send(kind, tool_call_id=tool_id, delta="private query and evidence" * 10000) == []
        assert send("TOOL_CALL_END", tool_call_id=tool_id) == []
        assert send("TOOL_RESULT_END", tool_call_id=tool_id, state="success") == []
    assert not state.tool_result_buffers
    assert len(frames) == 1
    frames += send("REPLY_END", finished_reason="completed")
    assert frames[1]["value"]["search_count"] == 20
    assert "private" not in json.dumps(frames)
    assert len(json.dumps(frames)) < 1500


def test_sources_are_deduplicated_and_only_locations_survive_resume():
    state = TranslationState(active_reply_id="r")
    source = {"source_id": "s", "knowledge_base_id": "k", "document_id": "d",
              "filename": "manual.pdf", "chunk_index": 2, "excerpt": "private evidence",
              "metadata": {"page": 3, "secret": "hidden"}, "score": .8}
    event = {"type": "CUSTOM", "name": "knowledge_sources",
             "value": {"reply_id": "r", "sources": [source, source]}}
    translate_event(event, "c", state)
    translate_event({"type": "REPLY_START", "reply_id": "r"}, "c", state)
    assert translate_event(event, "c", state) == []
    frames = translate_event({"type": "REPLY_END", "reply_id": "r"}, "c", state)
    sources = next(e for e in frames if e.get("name") == "knowledge_sources")["value"]["sources"]
    assert len(sources) == 1
    assert sources[0]["metadata"] == {"page": 3}
    assert "excerpt" not in sources[0] and "score" not in sources[0]
    translate_event({"type": "REPLY_START", "reply_id": "next"}, "c", state)
    assert not state.knowledge.sources


def test_foreign_static_retrieval_rejected_and_errors_are_explicit():
    state = TranslationState(active_reply_id="r")
    event = {"type": "CUSTOM", "name": "knowledge_retrieval",
             "value": {"reply_id": "foreign", "phase": "finished", "status": "error", "query": "private"}}
    assert translate_event(event, "c", state) == []
    event["value"]["reply_id"] = "r"
    frames = translate_event(event, "c", state)
    assert frames[-1]["value"]["status"] == "error"
    assert frames[-1]["value"]["failure_count"] == 1
    assert "private" not in json.dumps(frames)
    assert translate_event(event, "c", state) == []


def test_trimmed_replay_without_tool_name_does_not_leak_orphan_results():
    state = TranslationState(active_reply_id="recovered")
    assert translate_event({"type": "TOOL_RESULT_TEXT_DELTA", "reply_id": "recovered",
                            "tool_call_id": "orphan", "delta": "private evidence"}, "c", state) == []
    assert not state.tool_result_buffers
    assert translate_event({"type": "TOOL_RESULT_END", "reply_id": "recovered",
                            "tool_call_id": "orphan", "state": "error"}, "c", state) == []
    frames = translate_event({"type": "REPLY_END", "reply_id": "recovered"}, "c", state)
    assert any(frame["type"] == "RUN_ERROR" for frame in frames)


def test_durable_translation_roundtrip_preserves_retrieval_and_skill_classification():
    from workmate_product.projections.translator import translation_from_json, translation_to_json
    state = TranslationState()
    for raw in [
        {'type': 'REPLY_START'},
        {'type': 'TOOL_CALL_START', 'tool_call_id': 'rag', 'tool_call_name': 'search_knowledge'},
        {'type': 'TOOL_CALL_START', 'tool_call_id': 'skill', 'tool_call_name': 'Skill'},
        {'type': 'CUSTOM', 'name': 'knowledge_sources', 'value': {'reply_id': 'r', 'sources': [{'source_id': 's', 'filename': 'doc'}]}},
    ]:
        translate_event({'reply_id': 'r', **raw}, 'c', state)
    restored = translation_from_json(json.loads(json.dumps(translation_to_json(state))))
    for identity in ('rag', 'skill'):
        assert translate_event({'type': 'TOOL_RESULT_TEXT_DELTA', 'reply_id': 'r',
                                'tool_call_id': identity, 'delta': 'secret'}, 'c', restored) == []
    output = translate_event({'type': 'REPLY_END', 'reply_id': 'r', 'finished_reason': 'completed'}, 'c', restored)
    sources = next(item['value']['sources'] for item in output if item.get('name') == 'knowledge_sources')
    assert sources[0]['filename'] == 'doc'


async def test_native_projection_persists_compact_browser_and_full_retrieval_audit(
    db_session, seeded_ids,
):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.adapter.durable_projection import live_projector
    from workmate_product.projections.translator import translation_to_json
    _, conversation_id = seeded_ids
    task = Task(id=str(uuid.uuid4()), conversation_id=conversation_id, status='running', title='Native', run_id='r')
    db_session.add(task)
    await db_session.flush()
    state = {'actor_user_id': 'u', 'translation': translation_to_json(TranslationState(active_reply_id='r'))}
    projector = live_projector()
    public = []
    for raw in [
        {'type': 'TOOL_CALL_START', 'tool_call_id': 'rag', 'tool_call_name': 'search_knowledge'},
        {'type': 'TOOL_CALL_DELTA', 'tool_call_id': 'rag', 'delta': 'private query'},
        {'type': 'TOOL_RESULT_TEXT_DELTA', 'tool_call_id': 'rag', 'delta': 'private evidence'},
        {'type': 'TOOL_RESULT_END', 'tool_call_id': 'rag', 'state': 'success'},
        {'type': 'CUSTOM', 'name': 'knowledge_retrieval', 'value': {'reply_id': 'r', 'phase': 'finished', 'status': 'success', 'query': 'private query'}},
        {'type': 'TOOL_CALL_START', 'tool_call_id': 'skill', 'tool_call_name': 'Skill'},
        {'type': 'TOOL_RESULT_TEXT_DELTA', 'tool_call_id': 'skill', 'delta': 'private instructions'},
        {'type': 'TOOL_RESULT_END', 'tool_call_id': 'skill', 'state': 'success'},
    ]:
        raw = {'reply_id': 'r', **raw}
        event = SimpleNamespace(product_task_id=task.id, conversation_id=conversation_id,
                                event_type=raw['type'], reply_id='r', payload=raw,
                                occurred_at=datetime.now(timezone.utc))
        result = await projector(db_session, event, state, None)
        state = json.loads(json.dumps(result.state))
        public.extend(item.payload for item in result.events)
    assert 'private' not in json.dumps(public)
    audits = (await db_session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id))).all()
    retrieval = next(row for row in audits if row.event_type == 'knowledge_retrieval')
    assert retrieval.payload['query'] == 'private query'
    assert 'private' not in json.dumps(retrieval.payload['browser_projection'])
    result = next(row for row in audits if row.event_type == 'tool_result' and row.payload['tool'] == 'search_knowledge')
    assert result.payload['arguments'] == 'private query'
    skill = next(row for row in audits if row.event_type == 'tool_result' and row.payload['tool'] == 'Skill')
    assert skill.payload['content_hidden'] is True
