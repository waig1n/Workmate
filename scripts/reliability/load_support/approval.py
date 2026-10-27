"""Drive one real Product approval; never fabricate a resumed execution."""
import asyncio
import re
import time


def fixture_state(messages, fallback):
    for index,message in reversed(list(enumerate(messages))):
        found=None
        for block in message.content if isinstance(message.content,list) else []:
            found=re.fullmatch(r'fixture-case:\w+ ([0-9a-f]{32})',getattr(block,'text',''))
            if found:break
        if found:
            fallback=found[1];messages=messages[index:];break
    for message in reversed(messages):
        for block in reversed(message.content if isinstance(message.content,list) else []):
            identifier=getattr(block,'id','')
            if getattr(block,'type',None)=='tool_result' and re.fullmatch('write-[0-9a-f]{32}',identifier):
                return identifier[6:],True
    return fallback,False


async def approve_once(http, task_id, headers, *, delay=5, tool='Write'):
    async with asyncio.timeout(45):
        while True:
            response=await http.get('/api/tasks/'+task_id+'/pending-confirm',headers=headers)
            if response.status_code==200:break
            assert response.status_code==404,response.status_code
            await asyncio.sleep(.1)
        pending=response.json()
        calls=pending['toolCalls']
        assert len(calls)==1 and calls[0]['name']==tool,pending
        observed=time.monotonic_ns()
        await asyncio.sleep(delay)
        body={'decision_version':pending['decisionVersion'],
              'decisions':[{'tool_call_id':calls[0]['id'],'approved':True}]}
        response=await http.post('/api/tasks/'+task_id+'/confirm',headers=headers,json=body)
        assert response.status_code==200,response.text
        return {'decision_version':pending['decisionVersion'],'call_id':calls[0]['id'],
                'wait_seconds':(time.monotonic_ns()-observed)/1e9}
