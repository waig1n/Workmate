"""The final result must fit the replay envelope, including JSON escaping."""
import json

import pytest

from workmate_product.projections.translator import TranslationState, translate_event
from workmate_product.projections.store import ProjectedEvent, _items


@pytest.mark.parametrize('chunk', ['x'*80000, '汉字'*20000, '\\"\n'*20000, '🙂'*20000], ids=['ascii', 'chinese', 'escaped', 'emoji'])
def test_large_result_summary_fits_event_limit_without_changing_deltas(chunk):
    state = TranslationState()
    translate_event({'type':'TOOL_CALL_START','tool_call_id':'call','tool_call_name':'Bash','reply_id':'r'}, 'c', state)
    for _ in range(5):
        events = translate_event({'type':'TOOL_RESULT_TEXT_DELTA','tool_call_id':'call',
                                  'reply_id':'r','delta':chunk}, 'c', state)
        assert events[0]['value']['delta'] == chunk
    events = translate_event({'type':'TOOL_RESULT_END','tool_call_id':'call','reply_id':'r',
                              'state':'success'}, 'c', state)
    result = next(e for e in events if e['type'] == 'TOOL_CALL_RESULT')
    assert len(json.dumps(result, allow_nan=False, sort_keys=True).encode()) <= 262144
    assert '已截断' in result['content']
    assert (chunk*5).startswith(result['content'].split('\n\n[工具结果过长')[0])
    assert not state.tool_result_buffers
    _items([ProjectedEvent(key=str(i), event_type=e['type'], payload=e) for i,e in enumerate(events)])


def test_small_result_and_error_status_remain_unchanged():
    state = TranslationState()
    translate_event({'type':'TOOL_CALL_START','tool_call_id':'call','tool_call_name':'Bash'}, 'c', state)
    translate_event({'type':'TOOL_RESULT_TEXT_DELTA','tool_call_id':'call','delta':'error text'}, 'c', state)
    events = translate_event({'type':'TOOL_RESULT_END','tool_call_id':'call','state':'error'}, 'c', state)
    assert events[0]['content'] == 'error text'
    assert events[1]['value']['state'] == 'error'
