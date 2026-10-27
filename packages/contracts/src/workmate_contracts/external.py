"""Engine-neutral, bounded terminal results from authenticated external producers."""
from typing import Annotated,Literal
from pydantic import Field,JsonValue,model_validator
from . import Contract,Identity,MAX_PAYLOAD_BYTES,snapshot_digest
from .controls import ControlPayload
import json


class ExternalResultReceipt(Contract):
    receipt_id: Identity
    producer_id: Identity
    job_id: Identity
    conversation_id: Identity
    session_id: Identity
    task_id: Identity
    reply_id: Identity
    execution_epoch: Annotated[int,Field(ge=1)]
    tool_call_id: Identity
    tool_name: Identity
    input_digest: str=Field(pattern=r'^[0-9a-f]{64}$')
    result: dict[str,JsonValue]

    @model_validator(mode='after')
    def exact_terminal_result(self):
        result=self.result
        if (set(result)!={'type','id','name','output','state','metadata','created_at','finished_at'}
                or result['type']!='tool_result' or result['id']!=self.tool_call_id or result['name']!=self.tool_name
                or result['state'] not in {'success','error','denied','interrupted'}
                or not isinstance(result['output'],(str,list)) or not isinstance(result['metadata'],dict)
                or not isinstance(result['created_at'],str)
                or (result['finished_at'] is not None and not isinstance(result['finished_at'],str))
                or len(json.dumps(self.model_dump(),ensure_ascii=False).encode())>MAX_PAYLOAD_BYTES):
            raise ValueError('External receipt requires an exact bounded terminal ToolResult')
        return self

    def digest(self):
        return snapshot_digest(self.model_dump(mode='json'))


class ExternalResumePayload(ControlPayload):
    resume_kind: Literal['external_result']
    receipt_id: Identity
    snapshot_id: Identity
    state_version: Annotated[int,Field(ge=1)]
    full_access: Literal[False]=False
