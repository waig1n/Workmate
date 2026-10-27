"""Test-only real Core trace for backend projection tests without API engine imports."""
import asyncio
import json
from pathlib import Path
import sys

from agentscope.event import ConfirmResult, UserConfirmResultEvent
from agentscope.message import ToolCallBlock, UserMsg
from agentscope.state import AgentState
from test_core_engine import ConfirmTool, collect, make_engine, json_state


async def main():
    external=len(sys.argv)>2 and sys.argv[2]=="external"
    tool=ConfirmTool()
    tool.is_external_tool=external
    tool.is_concurrency_safe=True
    tool.safety_ask=True
    engine=make_engine([[ToolCallBlock(id=cid,name=tool.name,input='{}') for cid in (('a',) if external else ('a','b'))]],
                       state=AgentState(session_id='s'),tool=tool)
    events=await collect(engine,UserMsg(name='user',content='write twice'))
    calls=[call for event in events if event.type=='REQUIRE_USER_CONFIRM' for call in event.tool_calls]
    def trace(current,events):
        snapshot=current.snapshot()
        return {'events':[e.model_dump(mode='json') for e in events],
                'snapshot':snapshot.model_dump(mode='json'),
                'evidence':snapshot.boundary_evidence('test').model_dump(mode='json')}
    initial=trace(engine,events)
    restored=make_engine([],state=json_state(engine),tool=tool)
    events=await collect(restored,UserConfirmResultEvent(reply_id=engine.agent.state.reply_id,
        confirm_results=[ConfirmResult(confirmed=True,tool_call=next(c for c in calls if c.id=='a'))]))
    assert tool.calls==(0 if external else 1)
    assert not any(e.type=='REQUIRE_USER_CONFIRM' for e in events)
    Path(sys.argv[1]).write_text(json.dumps([initial,trace(restored,events)]))


if __name__=='__main__':
    asyncio.run(main())
