import asyncio
from types import SimpleNamespace

import pytest

from scripts.reliability.load_support.approval import approve_once, fixture_state


def test_resume_recovers_key_and_does_not_emit_write_again():
    key='a'*32
    messages=[SimpleNamespace(content=[SimpleNamespace(type='tool_result',id='write-'+key)])]
    assert fixture_state(messages,'b'*32)==(key,True)
    assert fixture_state([],key)==(key,False)


def test_approve_records_version_and_exact_call():
    class HTTP:
        async def get(self,*args,**kwargs):
            return SimpleNamespace(status_code=200,json=lambda:{'decisionVersion':'v1','toolCalls':[{'id':'write-a','name':'Write'}]})
        async def post(self,*args,**kwargs):
            assert kwargs['json']=={'decision_version':'v1','decisions':[{'tool_call_id':'write-a','approved':True}]}
            return SimpleNamespace(status_code=200,text='')
    assert asyncio.run(approve_once(HTTP(),'task',{},delay=0))['decision_version']=='v1'


def test_approval_rejects_unexpected_tool():
    class HTTP:
        async def get(self,*args,**kwargs):
            return SimpleNamespace(status_code=200,json=lambda:{'decisionVersion':'v','toolCalls':[{'id':'x','name':'Bash'}]})
    with pytest.raises(AssertionError):asyncio.run(approve_once(HTTP(),'task',{},delay=0))


def test_new_task_does_not_reuse_previous_tool_result():
    old='a'*32;new='b'*32
    messages=[SimpleNamespace(content=[SimpleNamespace(type='tool_result',id='write-'+old)]),
        SimpleNamespace(content=[SimpleNamespace(text='fixture-case:file '+new)])]
    assert fixture_state(messages,old)==(new,False)
    messages.append(SimpleNamespace(content=[SimpleNamespace(type='tool_result',id='write-'+new)]))
    assert fixture_state(messages,old)==(new,True)
