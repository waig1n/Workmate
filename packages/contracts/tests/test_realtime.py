"""Wire contract and reducer checks shared with the TypeScript consumer."""
import json
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError
from workmate_contracts.realtime import (
    RealtimeCursor, RealtimeEnvelope, MessageSnapshot, RealtimeReset, TextState,
    apply_text_delta, classify_resume, decode_cursor, encode_cursor, message_id,
)

FIXTURE=Path(__file__).parents[1]/'fixtures'/'realtime_v1.json'
GEN='00000000-0000-4000-8000-000000000001'


def cursor(**changes):
    return RealtimeCursor(conversation_id='c',generation=GEN,stream_id='10-2',**changes)


def envelope(**changes):
    value=dict(event_id='event-1',conversation_id='c',task_id='t',reply_id='r',source='worker',
        execution_epoch=1,payload={'type':'TEXT_MESSAGE_CONTENT','delta':'你'},
        text=dict(message_id=message_id('t','r'),block_id='b',content_version=1,from_offset=0,to_offset=3))
    value.update(changes)
    return RealtimeEnvelope(**value)


def test_cursor_canonical_roundtrip_and_resume_boundaries():
    token=encode_cursor(cursor())
    assert decode_cursor(token,conversation_id='c')==cursor()
    kwargs=dict(conversation_id='c',generation=GEN,first_stream_id='10-0',last_stream_id='20-0')
    assert classify_resume(token,**kwargs).action=='resume'
    assert classify_resume(token,**{**kwargs,'first_stream_id':'11-0'}).reason=='trimmed'
    assert classify_resume(token,**{**kwargs,'generation':'00000000-0000-4000-8000-000000000002'}).reason=='generation_changed'
    legacy='product-'+hashlib.sha256(b'c').hexdigest()[:32]+':123'
    assert classify_resume(legacy,**kwargs).reason=='legacy_cursor'
    with pytest.raises(ValueError): classify_resume(legacy,**{**kwargs,'conversation_id':'other'})
    with pytest.raises(ValueError): classify_resume('123',**kwargs)
    with pytest.raises(ValueError): classify_resume(token,**{**kwargs,'conversation_id':'other'})
    with pytest.raises(ValueError): classify_resume(token,**{**kwargs,'last_stream_id':'10-1'})


@pytest.mark.parametrize('token',['','rt1.!!!!','rt1.e30=','1:22','rt2.abc','rt1.'+'A'*2048])
def test_malformed_cursor_rejected(token):
    with pytest.raises(ValueError): decode_cursor(token)


@pytest.mark.parametrize('field,value',[
    ('stream_id','01-0'),('stream_id','18446744073709551616-0'),('stream_id','1--1'),
    ('generation','not-a-uuid'),('schema_version',2),('schema_version',True),
    ('conversation_id','\ud800'),('conversation_id',''),
])
def test_cursor_strict(field,value):
    data=cursor().model_dump();data[field]=value
    with pytest.raises(ValueError): RealtimeCursor.model_validate(data)


def test_cursor_rejects_noncanonical_json_and_duplicate_keys():
    import base64
    for raw in ['{"conversation_id":"c", "generation":"'+GEN+'","schema_version":1,"stream_id":"10-2"}',
                '{"conversation_id":"x","conversation_id":"c","generation":"'+GEN+'","schema_version":1,"stream_id":"10-2"}']:
        token='rt1.'+base64.urlsafe_b64encode(raw.encode()).decode().rstrip('=')
        with pytest.raises(ValueError): decode_cursor(token)


def test_envelope_source_and_utf8_range_validation():
    assert envelope().text.to_offset==3
    assert envelope(payload={'type':'REASONING_MESSAGE_CONTENT','delta':'你'}).text.to_offset==3
    for changes in [dict(execution_epoch=0),dict(business_seq=1),dict(task_id=None),dict(reply_id=None),
                    dict(text=None),dict(text={**envelope().text.model_dump(),'to_offset':1}),
                    dict(payload={'type':'TEXT_MESSAGE_CONTENT','delta':'\ud800'}),
                    dict(payload={'type':'X','value':float('nan')}),
                    dict(payload={'type':'X','value':2**53}),dict(protocol_version=1)]:
        with pytest.raises(ValueError): envelope(**changes)
    business=RealtimeEnvelope(event_id='e',conversation_id='c',source='business',business_seq=1,payload={'type':'CUSTOM'})
    assert business.execution_epoch is None
    with pytest.raises(ValueError): RealtimeEnvelope(**{**business.model_dump(),'business_seq':None})


def test_snapshot_identity_and_scope_validation():
    snapshot=MessageSnapshot(message_id=message_id('t','r'),conversation_id='c',task_id='t',reply_id='r',
        execution_epoch=1,snapshot_version=1,blocks=[dict(block_id='b',kind='text',text='你好😀',content_version=1,closed=True)],
        covered_cursor=cursor(),business_seq=1,phase='final',incomplete=False,process_end=3)
    assert snapshot.process_end==3 # codepoints, not 10 UTF8 bytes
    with pytest.raises(ValueError): MessageSnapshot(**{**snapshot.model_dump(),'message_id':message_id('other','r')})
    with pytest.raises(ValueError): MessageSnapshot(**{**snapshot.model_dump(),'blocks':snapshot.model_dump()['blocks']*2})
    with pytest.raises(ValueError): MessageSnapshot(**{**snapshot.model_dump(),'covered_cursor':{**cursor().model_dump(),'conversation_id':'other'}})
    with pytest.raises(ValueError): MessageSnapshot(**{**snapshot.model_dump(),'blocks':[{**snapshot.blocks[0].model_dump(),'closed':False}]})
    reset=RealtimeReset(conversation_id='c',generation=GEN,reason='initial',snapshots=[snapshot],business_state={},business_seq=1,sync_status='synced')
    with pytest.raises(ValueError): RealtimeReset(**{**reset.model_dump(),'conversation_id':'other'})


def test_shared_golden_fixtures():
    cases=json.loads(FIXTURE.read_text())
    for case in cases['cursors']:
        assert encode_cursor(RealtimeCursor.model_validate(case['value']))==case['token']
        assert decode_cursor(case['token']).model_dump()==case['value']
    for case in cases['message_ids']:
        assert message_id(case['task_id'],case['reply_id'])==case['message_id']
    for case in cases['deltas']:
        result=apply_text_delta(TextState.model_validate(case['state']),RealtimeEnvelope.model_validate(case['envelope']))
        assert result.model_dump()==case['expected'],case['name']
    for case in cases['resume']:
        assert classify_resume(case['token'],**case['args']).model_dump()==case['expected']
    for case in cases['snapshots']: MessageSnapshot.model_validate(case)
    for case in cases['resets']: RealtimeReset.model_validate(case)


def test_stream_ids_preserve_full_redis_uint64_without_float_rounding():
    high=RealtimeCursor(conversation_id='c',generation=GEN,stream_id='18446744073709551615-18446744073709551615')
    assert decode_cursor(encode_cursor(high))==high
    assert classify_resume(encode_cursor(high),conversation_id='c',generation=GEN,
        first_stream_id='18446744073709551615-18446744073709551614',
        last_stream_id=high.stream_id).action=='resume'


@pytest.mark.parametrize('changes',[
    {'execution_epoch':True}, {'execution_epoch':2**53},
    {'payload':{'type':'X','number':10**500}},
    {'text':{'message_id':message_id('t','r'),'block_id':'b','content_version':True,'from_offset':0,'to_offset':3}},
    {'payload':{'type':'TEXT_MESSAGE_CONTENT','delta':'你','extra':{'secret':'\0'}}},
    {'payload':{'type':'TEXT_MESSAGE_CONTENT','delta':'你','extra':'x'*262_144}},
])
def test_envelope_rejects_unsafe_or_unbounded_values(changes):
    with pytest.raises(ValueError): envelope(**changes)


def test_contracts_are_frozen_and_do_not_accept_internal_protocol_fields():
    from workmate_contracts.realtime import ContentBlock
    value=cursor()
    with pytest.raises(ValidationError): value.stream_id='20-0'
    with pytest.raises(ValueError): ContentBlock(block_id='b',kind='text',text='',content_version=0,closed=False,protocol_version=1)


def test_reducer_never_mutates_input_and_checks_same_version_overlap_conflict():
    current=TextState(message_id=message_id('t','r'),block_id='b',content='好',content_version=1,
        end_utf8=3,closed=False,execution_epoch=1)
    before=current.model_dump()
    result=apply_text_delta(current,envelope())
    assert result.action=='reset' and result.reason=='content_conflict'
    assert current.model_dump()==before


def nested_payload(leaf_depth):
    value='leaf'
    # envelope root=0, payload=1, payload.extra=2.
    for _ in range(leaf_depth-2): value={'nested':value}
    return {'type':'CUSTOM','extra':value}


def test_json_depth_counts_whole_wire_root_and_bounds_at_64():
    RealtimeEnvelope(event_id='e',conversation_id='c',source='business',business_seq=1,payload=nested_payload(64))
    with pytest.raises(ValueError,match='depth'):
        RealtimeEnvelope(event_id='e',conversation_id='c',source='business',business_seq=1,payload=nested_payload(65))


def test_synced_reset_cannot_claim_incomplete_snapshot_is_current():
    data=json.loads(FIXTURE.read_text())['resets'][0]
    data['snapshots'][0]['incomplete']=True
    data['snapshots'][0]['phase']='interrupted'
    with pytest.raises(ValueError,match='incomplete'):
        RealtimeReset.model_validate(data)
    data['sync_status']='incomplete'
    RealtimeReset.model_validate(data)


def test_shared_negative_and_depth_fixtures():
    cases=json.loads(FIXTURE.read_text())
    for case in cases['valid_envelopes']:
        RealtimeEnvelope.model_validate(case['value'])
    for kind,model in [('envelopes',RealtimeEnvelope),('snapshots',MessageSnapshot),('resets',RealtimeReset)]:
        for case in cases['invalid_'+kind]:
            with pytest.raises(ValueError): model.model_validate(case['value'])


def test_final_snapshot_must_be_complete_but_interruption_can_preserve_fragment():
    value=json.loads(FIXTURE.read_text())['snapshots'][0]
    value['incomplete']=True
    with pytest.raises(ValueError,match='Final snapshot'):
        MessageSnapshot.model_validate(value)
    value['phase']='interrupted'
    assert MessageSnapshot.model_validate(value).incomplete is True


@pytest.mark.parametrize('case', json.loads(FIXTURE.read_text())['payload_budget_cases'], ids=lambda case: case['name'])
def test_shared_payload_budget_leaves_room_for_envelope_metadata(case):
    payload = {'type': 'CUSTOM', 'name': 'RESULT', 'value': ''}
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
    remaining = case['payload_bytes'] - len(encode(payload))
    count, rest = divmod(remaining, len(case['fill'].encode()))
    payload['value'] = case['fill'] * count + 'x' * rest
    assert len(encode(payload)) == case['payload_bytes']
    value = dict(event_id='e' * 128, conversation_id='c' * 128, task_id='t' * 128,
                 reply_id='r' * 128, source='business', business_seq=1, payload=payload)
    if case['valid']:
        event = RealtimeEnvelope(**value)
        assert event.payload == payload
        assert 262144 < len(encode(event.model_dump())) <= 266240
    else:
        with pytest.raises(ValueError):
            RealtimeEnvelope(**value)


@pytest.mark.parametrize('case', json.loads(FIXTURE.read_text())['reset_anchor_cases'], ids=lambda case: case['name'])
def test_shared_reset_requires_current_anchor_only_for_synced_nonterminal(case):
    value = json.loads(FIXTURE.read_text())['resets'][0]
    snapshot = value['snapshots'][0]
    snapshot['phase'] = case['phase']
    value['sync_status'] = case['sync_status']
    if case['anchor'] == 'none':
        snapshot['covered_cursor'] = None
    elif case['anchor'] == 'old_generation':
        value['generation'] = '00000000-0000-4000-8000-000000000002'
    if not case['valid']:
        with pytest.raises(ValueError):
            RealtimeReset.model_validate(value)
    else:
        parsed = RealtimeReset.model_validate(value)
        # Lacking a tail proof does not imply lost body content.
        assert parsed.snapshots[0].blocks[0].text == snapshot['blocks'][0]['text']
        assert parsed.snapshots[0].incomplete is False
        assert parsed.reason == 'initial'
        assert parsed.model_dump() == value


def test_display_snapshot_reference_is_exact_and_contains_no_body():
    from workmate_contracts.realtime import MessageSnapshotReference
    value=json.loads(FIXTURE.read_text())['snapshots'][0]
    cut=MessageSnapshot.model_validate(value)
    reference=MessageSnapshotReference.from_snapshot(cut)
    assert set(reference.model_dump())=={'message_id','snapshot_version','execution_epoch','sha256'}
    assert reference==MessageSnapshotReference.from_snapshot(MessageSnapshot.model_validate(value))
    assert reference!=MessageSnapshotReference.from_snapshot(cut.model_copy(update={
        'snapshot_version':cut.snapshot_version+1}))
    changed=cut.model_copy(update={'blocks':[
        *cut.blocks[:-1],cut.blocks[-1].model_copy(update={'text':cut.blocks[-1].text+'changed'})]})
    assert reference.sha256!=MessageSnapshotReference.from_snapshot(changed).sha256
    for changes in ({'sha256':'a'*63},{'sha256':'A'*64},{'sha256':'a'*64+'\n'},
                    {'snapshot_version':0},{'execution_epoch':True},{'body':'private'}):
        with pytest.raises(ValueError):
            MessageSnapshotReference.model_validate({**reference.model_dump(),**changes})
