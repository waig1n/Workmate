"""Pure body accumulation: identity, atomic failure, and restart boundaries."""
import pytest
from workmate_contracts.realtime import MAX_PAYLOAD_BYTES
from workmate_product.realtime.message_accumulator import MessageAccumulator


def accumulator(**kw):
    return MessageAccumulator('c', 't', 'r', 1, **kw)


def event(kind='TEXT', action='START', block='b', **kw):
    return {'type': f'{kind}_BLOCK_{action}', 'reply_id': 'r', 'block_id': block, **kw}


def capture(a, phase='partial', **kw):
    return a.capture(phase, None, 0, **kw)


def test_unicode_ranges_versions_and_process_split():
    a=accumulator()
    assert a.apply(event())[0].payload['type']=='TEXT_MESSAGE_START'
    delta=a.apply(event(action='DELTA',delta='你😀'))[0]
    assert (delta.text.from_offset,delta.text.to_offset,delta.text.content_version)==(0,7,1)
    assert a.apply({'type':'TOOL_CALL_START','reply_id':'r'})==()
    a.apply(event('THINKING',block='thought'))
    a.apply(event('THINKING','DELTA','thought',delta='why'))
    a.apply(event(action='DELTA',delta='好'))
    snap=capture(a)
    assert snap.process_end==2
    assert [(b.kind,b.text,b.content_version) for b in snap.blocks]==[('text','你😀好',2),('reasoning','why',1)]
    assert snap.snapshot_version==1
    assert capture(a).snapshot_version==2


def test_empty_delta_idempotent_boundaries_and_closed_block():
    a=accumulator()
    first=a.apply(event())
    assert a.apply(event())==()
    assert a.apply(event(action='DELTA'))[0].text.content_version==1
    assert a.apply(event(action='END'))
    assert a.apply(event(action='END'))==()
    assert a.apply(event())==()
    with pytest.raises(ValueError): a.apply(event(action='DELTA',delta='late'))
    b=accumulator()
    assert b.apply(event())[0].event_id==first[0].event_id


@pytest.mark.parametrize('raw', [None, [], {'type':'TEXT_BLOCK_DELTA','reply_id':'r','block_id':'b','delta':'orphan'},
    event(reply_id='other'),event(block=''),event(delta=float('nan')),
    event(extra='\ud800'),event(extra='\0'),event(action='DELTA',delta='x'*MAX_PAYLOAD_BYTES)])
def test_invalid_input_does_not_partially_mutate(raw):
    a=accumulator()
    with pytest.raises((ValueError,TypeError)): a.apply(raw)
    assert capture(a).blocks==[]


def test_failed_delta_leaves_existing_text_and_version_intact():
    a=accumulator();a.apply(event())
    for delta in (None, 123, '\ud800', 'x'*MAX_PAYLOAD_BYTES):
        with pytest.raises((ValueError,TypeError)): a.apply(event(action='DELTA',delta=delta))
    assert a.apply(event(action='DELTA',delta='ok'))[0].text.content_version==1


def test_restore_same_reply_resume_preserves_order_versions_and_copies():
    a=accumulator();a.apply(event());a.apply(event(action='DELTA',delta='old'))
    old=capture(a,'approval')
    b=MessageAccumulator('c','t','r',2,restored_snapshot=old)
    assert b.apply({'type':'REPLY_START','reply_id':'r'})==()
    assert b.apply(event(action='DELTA',delta='new'))[0].text.content_version==2
    current=capture(b)
    assert current.blocks[0].text=='oldnew'
    assert old.blocks[0].text=='old'
    current.blocks.clear()
    assert capture(b).blocks[0].text=='oldnew'
    assert capture(b).snapshot_version==4
    for args in [('other','t','r',2),('c','other','r',2),('c','t','other',2),('c','t','r',0)]:
        with pytest.raises(ValueError): MessageAccumulator(*args,restored_snapshot=old)


@pytest.mark.parametrize('phase',['final','interrupted'])
def test_terminal_capture_closes_and_cannot_resume(phase):
    a=accumulator();a.apply(event())
    snap=capture(a,phase)
    assert snap.blocks[0].closed
    for target in (a,MessageAccumulator('c','t','r',2,restored_snapshot=snap)):
        with pytest.raises(ValueError): target.apply(event(action='DELTA',delta='late'))
        with pytest.raises(ValueError): capture(target,'partial')


def test_failed_capture_does_not_close_or_increment():
    a=accumulator();a.apply(event())
    with pytest.raises(ValueError): capture(a,'final',incomplete=True)
    a.apply(event(action='DELTA',delta='still open'))
    assert capture(a).snapshot_version==1


def test_block_kind_identity_and_missing_start_are_rejected():
    a=accumulator();a.apply(event())
    for raw in (event('THINKING'),event(action='END',block='missing'),
                {'type':'MODEL_CALL_START','reply_id':'foreign'},event(task_id='foreign')):
        with pytest.raises(ValueError): a.apply(raw)
    assert a.apply({'type':'MODEL_CALL_START','reply_id':'r'})==()
    assert capture(a).blocks[0].kind=='text'


def test_capture_watermarks_are_monotonic_and_failed_cut_is_atomic():
    from workmate_contracts.realtime import RealtimeCursor
    from uuid import uuid4
    a=accumulator()
    cursor=RealtimeCursor(conversation_id='c',generation=str(uuid4()),stream_id='10-0')
    first=a.capture('partial',cursor,5)
    for coverage, seq in ((None,5),(cursor,4),(cursor.model_copy(update={'stream_id':'9-0'}),5),
                          (cursor.model_copy(update={'conversation_id':'foreign'}),5)):
        with pytest.raises(ValueError): a.capture('final',coverage,seq)
    assert a.capture('partial',cursor,5).snapshot_version==first.snapshot_version+1


def test_older_execution_epoch_cannot_restore_snapshot():
    a=MessageAccumulator('c','t','r',3)
    with pytest.raises(ValueError): MessageAccumulator('c','t','r',2,restored_snapshot=capture(a))


def test_business_payload_is_not_revalidated_as_a_body_notification():
    a=accumulator()
    assert a.apply({'type':'TOOL_RESULT_END','reply_id':'r','content':'x'*(MAX_PAYLOAD_BYTES+1)})==()
    for raw in ({}, {'type':[]}, {'type':''}):
        with pytest.raises(ValueError): a.apply(raw)


def test_explicit_unanchored_cut_then_new_generation_preserves_content():
    from workmate_contracts.realtime import RealtimeCursor
    from uuid import uuid4
    a=accumulator();a.apply(event());a.apply(event(action='DELTA',delta='before'))
    old=RealtimeCursor(conversation_id='c',generation=str(uuid4()),stream_id='10-0')
    anchored=a.capture('partial',old,3)
    a.apply(event(action='DELTA',delta=' after'))
    for cursor, unanchored in ((None,False),(old,True),(None,1)):
        with pytest.raises(ValueError): a.capture('partial',cursor,4,unanchored=unanchored)
    disconnected=a.capture('partial',None,4,unanchored=True)
    assert disconnected.covered_cursor is None and not disconnected.incomplete
    assert disconnected.snapshot_version==anchored.snapshot_version+1
    assert disconnected.blocks[0].text=='before after'
    restored=MessageAccumulator('c','t','r',2,restored_snapshot=disconnected)
    new=RealtimeCursor(conversation_id='c',generation=str(uuid4()),stream_id='1-0')
    again=restored.capture('partial',new,4)
    assert again.snapshot_version==disconnected.snapshot_version+1
    assert again.blocks==disconnected.blocks
    assert again.covered_cursor==new


@pytest.mark.parametrize('phase',['final','interrupted'])
def test_terminal_cut_is_captured_once_and_retried_by_reusing_snapshot(phase):
    a=accumulator();first=capture(a,phase)
    for target in (a,MessageAccumulator('c','t','r',2,restored_snapshot=first)):
        with pytest.raises(ValueError): capture(target,phase)


def test_incomplete_restore_remains_incomplete_after_delta_and_cannot_finish():
    a=accumulator();a.apply(event());a.apply(event(action='DELTA',delta='partial'))
    lost=capture(a,incomplete=True)
    restored=MessageAccumulator('c','t','r',2,restored_snapshot=lost)
    restored.apply(event(action='DELTA',delta=' continued'))
    newer=capture(restored)
    assert newer.incomplete and newer.blocks[0].text=='partial continued'
    with pytest.raises(ValueError): capture(restored,'final')
    interrupted=capture(restored,'interrupted')
    assert interrupted.incomplete and interrupted.blocks[0].closed


def test_incomplete_current_state_is_sticky_and_requires_boolean():
    a=accumulator()
    assert capture(a,incomplete=True).incomplete
    for invalid in (0,1,None,'false'):
        with pytest.raises(ValueError): capture(a,incomplete=invalid)
    assert capture(a,incomplete=False).incomplete
