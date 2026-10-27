# -*- coding: utf-8 -*-
"""AgentScope event -> AG-UI event translation (design §6).

Standard lifecycle/message events keep the AG-UI standard names;
extensions ride on CustomEvent. Event names are never invented for
lifecycle semantics (design §22 #11).

The mapping mirrors AgentScope's built-in ``AGUIProtocolMiddleware``
where applicable (correction #9), but lives in the product layer so
product CustomEvents (TASK_TIMELINE / ARTIFACT_CREATED / HITL) can be
emitted on the same stream.

Input: parsed Core AgentEvent JSON dicts (``type`` discriminator field).
Shared by Product projection and the native Worker event outlet.
Output: AG-UI event dicts, ready to be serialized as SSE ``data:``.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from workmate_contracts import MAX_PAYLOAD_BYTES

from workmate_product.approval import approval_request_version
from .knowledge import KNOWLEDGE_CUSTOM_EVENTS, KnowledgeProjection, compact_sources, SOURCE_LIMIT

logger = logging.getLogger(__name__)

# AG-UI standard event types (subset used here).
RUN_STARTED = "RUN_STARTED"
RUN_FINISHED = "RUN_FINISHED"
RUN_ERROR = "RUN_ERROR"
RUN_CANCELLED = "RUN_CANCELLED"
TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
TOOL_CALL_START = "TOOL_CALL_START"
TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
TOOL_CALL_END = "TOOL_CALL_END"
TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
REASONING_MESSAGE_START = "REASONING_MESSAGE_START"
REASONING_MESSAGE_CONTENT = "REASONING_MESSAGE_CONTENT"
REASONING_MESSAGE_END = "REASONING_MESSAGE_END"
STEP_STARTED = "STEP_STARTED"
STEP_FINISHED = "STEP_FINISHED"
CUSTOM = "CUSTOM"

# Product extension CustomEvent names.
TASK_TIMELINE_EVENT = "TASK_TIMELINE_EVENT"
TASK_COMPLETED = "TASK_COMPLETED"
TOOL_RESULT_DELTA = "TOOL_RESULT_DELTA"
TOOL_RESULT_END = "TOOL_RESULT_END"
HITL_APPROVAL_REQUEST = "HITL_APPROVAL_REQUEST"
HITL_DECISION_RECORDED = "HITL_DECISION_RECORDED"
MODEL_CALL_END = "MODEL_CALL_END"
ARTIFACT_CREATED = "ARTIFACT_CREATED"
REPLAY_DONE = "REPLAY_DONE"

# AgentScope finished reasons that map onto a completed run
# (ReplyFinishedReason serializes as lowercase strings).
_INTERRUPTIVE_REASONS = {"interrupted", "exceed_max_iters"}
_IGNORED_REPLY_CACHE_MAX = 1024


def _evt(type_: str, **fields: Any) -> dict:
    return {"type": type_, **fields}


@dataclass
class TranslationState:
    """Per-upstream-stream state required by the AG-UI mapping."""

    last_model_name: str = "model_call"
    tool_result_buffers: dict[str, list[str]] = field(default_factory=dict)
    # reply_id of the run currently being translated. AgentScope 2.0.7
    # broadcasts a synthetic same-reply_id ReplyStartEvent when a parked
    # reply resumes (HITL confirmation); re-emitting RUN_STARTED would
    # invite AG-UI clients to clear their accumulated message state.
    active_reply_id: str | None = None
    # A different reply_id cannot start while the current top-level Reply is
    # still live or parked at HITL. AgentScope may synthesize a short-lived
    # setup-failure Reply for a rejected wake-up/input; those events belong to
    # the Session stream but not to the active Product Task.
    active_reply_terminal: bool = False
    ignored_reply_ids: dict[str, None] = field(default_factory=dict)
    last_tool_result_state: str | None = None
    knowledge: KnowledgeProjection = field(default_factory=KnowledgeProjection)
    known_tool_ids: set[str] = field(default_factory=set)
    skill_tool_ids: set[str] = field(default_factory=set)


def _bounded_tool_result(event: dict) -> dict:
    """Bound the final display result using the replay store's JSON encoding."""
    def size(value):
        return len(json.dumps(value, allow_nan=False, sort_keys=True).encode())

    if size(event) <= MAX_PAYLOAD_BYTES:
        return event
    suffix = "\n\n[工具结果过长，展示内容已截断]"
    content = event['content']
    low, high = 0, min(len(content), MAX_PAYLOAD_BYTES)
    while low < high:
        middle = (low + high + 1) // 2
        if size({**event, 'content': content[:middle] + suffix}) <= MAX_PAYLOAD_BYTES:
            low = middle
        else:
            high = middle - 1
    return {**event, 'content': content[:low] + suffix}


def translation_to_json(state: TranslationState) -> dict:
    """Persist classification with each source cursor, including across restarts."""
    payload = asdict(state)
    for name in ('known_tool_ids', 'skill_tool_ids'):
        payload[name] = sorted(payload[name], key=str)
    for name in ('tool_ids', 'finished_ids'):
        payload['knowledge'][name] = sorted(payload['knowledge'][name], key=str)
    return payload


def translation_from_json(payload: dict) -> TranslationState:
    values = dict(payload)
    knowledge = dict(values.pop('knowledge', {}))
    for name in ('tool_ids', 'finished_ids'):
        knowledge[name] = set(knowledge.get(name, []))
    for name in ('known_tool_ids', 'skill_tool_ids'):
        values[name] = set(values.get(name, []))
    return TranslationState(**values, knowledge=KnowledgeProjection(**knowledge))


def is_foreign_reply_event(event: dict, state: TranslationState) -> bool:
    """Return whether a reply-scoped event belongs to a side Reply.

    A new reply is accepted only after the current reply has reached its
    matching ``REPLY_END``. Same-reply ``REPLY_START`` remains the AgentScope
    2.0.7 HITL continuation signal.
    """
    reply_id = event.get("reply_id")
    if event.get("type") == "CUSTOM" and event.get("name") in KNOWLEDGE_CUSTOM_EVENTS:
        reply_id = event.get("value", {}).get("reply_id")
    if not reply_id:
        return False
    if reply_id in state.ignored_reply_ids:
        return True
    active_reply_id = state.active_reply_id
    if active_reply_id is None or reply_id == active_reply_id:
        return False
    if event.get("type") == "REPLY_START" and state.active_reply_terminal:
        return False

    state.ignored_reply_ids[reply_id] = None
    while len(state.ignored_reply_ids) > _IGNORED_REPLY_CACHE_MAX:
        state.ignored_reply_ids.pop(next(iter(state.ignored_reply_ids)))
    logger.warning(
        "ignoring foreign reply event %s for reply %s while %s is active",
        event.get("type"), reply_id, active_reply_id,
    )
    return True


def _block_id(event: dict) -> str:
    """Read the AgentScope block id, tolerating old captured fixtures."""
    block_id = event.get("block_id")
    if block_id:
        return block_id
    logger.warning(
        "AgentScope %s event has no block_id; falling back to reply_id",
        event.get("type"),
    )
    return event.get("reply_id", "")


def translate_event(
    event: dict,
    thread_id: str,
    state: TranslationState | None = None,
) -> list[dict]:
    """Translate one AgentScope event into 0..n AG-UI events.

    ``thread_id`` is the conversation id (AG-UI threadId).
    The AG-UI ``runId`` is the AgentScope ``reply_id`` (correction #1).
    """
    stateful = state is not None
    state = state or TranslationState()
    etype = event.get("type")
    out: list[dict] = []

    if etype == "CUSTOM" and event.get("name") in KNOWLEDGE_CUSTOM_EVENTS and not event.get("value", {}).get("reply_id"):
        return out
    if is_foreign_reply_event(event, state):
        return out

    knowledge = state.knowledge
    reply_id = event.get("reply_id") or state.active_reply_id
    tool_id = event.get("tool_call_id")
    if etype == "TOOL_CALL_START":
        state.known_tool_ids.add(tool_id)
    if etype == "TOOL_RESULT_END":
        state.last_tool_result_state = event.get("state", "success")
    if ((state.active_reply_id or (stateful and etype.startswith("TOOL_RESULT_")))
            and etype.startswith(("TOOL_CALL_", "TOOL_RESULT_"))
            and tool_id not in state.known_tool_ids):
        # A trimmed upstream replay can start inside a Skill/RAG result. Without
        # the named start there is no safe way to classify that payload.
        return []
    if etype == "TOOL_CALL_START" and event.get("tool_call_name") == "Skill":
        state.skill_tool_ids.add(tool_id)
    if tool_id in state.skill_tool_ids and etype.startswith("TOOL_RESULT_"):
        # Keep Runtime input intact; sanitize only the browser projection.
        state.tool_result_buffers.pop(tool_id, None)
        if etype != "TOOL_RESULT_END":
            return []
        status = event.get("state", "success")
        return [
            _evt(TOOL_CALL_RESULT, toolCallId=tool_id, messageId=reply_id,
                 content="Skill loaded" if status == "success" else "Skill could not be loaded"),
            _evt(CUSTOM, name=TOOL_RESULT_END, value={"toolCallId": tool_id, "state": status}),
            _evt(CUSTOM, name=TASK_TIMELINE_EVENT,
                 value={"phase": "tool_result_end", "toolCallId": tool_id, "state": status}),
        ]
    if etype == "TOOL_CALL_START" and event.get("tool_call_name") == "search_knowledge":
        knowledge.tool_ids.add(tool_id)
        return knowledge.start(reply_id)
    if tool_id in knowledge.tool_ids and etype.startswith(("TOOL_CALL_", "TOOL_RESULT_")):
        if etype == "TOOL_RESULT_END":
            state.last_tool_result_state = event.get("state", "success")
            return knowledge.finish(reply_id, tool_id, state.last_tool_result_state)
        return []
    if etype == "CUSTOM" and event.get("name") == "knowledge_retrieval":
        value = event.get("value", {})
        out.extend(knowledge.start(value["reply_id"]))
        if value.get("phase") == "finished":
            out.extend(knowledge.finish(value["reply_id"], "static", value.get("status", "error")))
        return out
    if etype == "CUSTOM" and event.get("name") == "knowledge_sources":
        out.extend(knowledge.start(event["value"]["reply_id"]))
        for source in compact_sources(event.get("value", {}).get("sources")):
            if len(knowledge.sources) < SOURCE_LIMIT:
                knowledge.sources[source["source_id"]] = source
        return out

    if etype == "REPLY_START":
        reply_id = event["reply_id"]
        is_resume = state.active_reply_id == reply_id
        if not is_resume:
            state.knowledge = KnowledgeProjection()
            state.known_tool_ids.clear()
            state.skill_tool_ids.clear()
            state.active_reply_id = reply_id
            state.active_reply_terminal = False
            state.last_tool_result_state = None
            out.append(
                _evt(
                    RUN_STARTED,
                    threadId=thread_id,
                    runId=reply_id,
                ),
            )
        out.append(
            _evt(
                CUSTOM,
                name=TASK_TIMELINE_EVENT,
                value={
                    "phase": "run_resumed" if is_resume else "run_started",
                    "replyId": reply_id,
                },
            ),
        )

    elif etype == "REPLY_END":
        out.extend(knowledge.end(event["reply_id"], event.get("finished_reason", "completed")))
        state.active_reply_terminal = True
        reason = event.get("finished_reason", "completed")
        error = event.get("error") or {}
        terminal_tool_failure = (
            reason == "completed"
            and state.last_tool_result_state
            in {"error", "interrupted", "denied"}
        )
        if terminal_tool_failure:
            out.append(
                _evt(
                    RUN_ERROR,
                    message="任务结束前的最后一次工具执行失败。",
                    code="incomplete_tool_execution",
                    # Scopes the error to its run: a replayed frame must
                    # not pose as the current run's failure.
                    runId=event["reply_id"],
                ),
            )
        elif reason == "error":
            out.append(
                _evt(
                    RUN_ERROR,
                    message=error.get("message", "run failed"),
                    code=error.get("type"),
                    runId=event["reply_id"],
                ),
            )
        elif reason in _INTERRUPTIVE_REASONS:
            out.append(_evt(RUN_CANCELLED, threadId=thread_id))
        else:
            out.append(
                _evt(RUN_FINISHED, threadId=thread_id, runId=event["reply_id"]),
            )
        out.append(
            _evt(
                CUSTOM,
                name=TASK_COMPLETED,
                value={
                    "reason": reason,
                    **(
                        {
                            "taskStatus": "failed",
                            "completionGate": "terminal_tool_failure",
                        }
                        if terminal_tool_failure
                        else {}
                    ),
                },
            ),
        )

    elif etype == "TEXT_BLOCK_START":
        out.append(
            _evt(
                TEXT_MESSAGE_START,
                messageId=_block_id(event),
                role="assistant",
            ),
        )

    elif etype == "TEXT_BLOCK_DELTA":
        out.append(
            _evt(
                TEXT_MESSAGE_CONTENT,
                messageId=_block_id(event),
                delta=event.get("delta", ""),
            ),
        )

    elif etype == "TEXT_BLOCK_END":
        out.append(
            _evt(TEXT_MESSAGE_END, messageId=_block_id(event)),
        )

    elif etype == "THINKING_BLOCK_START":
        out.append(
            _evt(
                REASONING_MESSAGE_START,
                messageId=_block_id(event),
                role="reasoning",
            ),
        )

    elif etype == "THINKING_BLOCK_DELTA":
        out.append(
            _evt(
                REASONING_MESSAGE_CONTENT,
                messageId=_block_id(event),
                delta=event.get("delta", ""),
            ),
        )

    elif etype == "THINKING_BLOCK_END":
        out.append(
            _evt(REASONING_MESSAGE_END, messageId=_block_id(event)),
        )

    elif etype == "MODEL_CALL_START":
        state.last_model_name = event.get("model_name") or "model_call"
        out.append(_evt(STEP_STARTED, stepName=state.last_model_name))

    elif etype == "MODEL_CALL_END":
        out.append(_evt(STEP_FINISHED, stepName=state.last_model_name))
        # Token usage is product data beyond the standard StepFinished shape.
        if event.get("input_tokens") is not None:
            out.append(
                _evt(
                    CUSTOM,
                    name=MODEL_CALL_END,
                    value={
                        "inputTokens": event.get("input_tokens"),
                        "outputTokens": event.get("output_tokens"),
                    },
                ),
            )

    elif etype == "TOOL_CALL_START":
        out.append(
            _evt(
                TOOL_CALL_START,
                toolCallId=event["tool_call_id"],
                toolCallName=event.get("tool_call_name", ""),
                parentMessageId=event.get("reply_id"),
            ),
        )

    elif etype == "TOOL_CALL_DELTA":
        out.append(
            _evt(
                TOOL_CALL_ARGS,
                toolCallId=event["tool_call_id"],
                delta=event.get("delta", ""),
            ),
        )

    elif etype == "TOOL_CALL_END":
        out.append(
            _evt(TOOL_CALL_END, toolCallId=event["tool_call_id"]),
        )
        out.append(
            _evt(
                CUSTOM,
                name=TASK_TIMELINE_EVENT,
                value={
                    "phase": "tool_call_end",
                    "toolCallId": event["tool_call_id"],
                },
            ),
        )

    elif etype == "TOOL_RESULT_TEXT_DELTA":
        state.tool_result_buffers.setdefault(
            event["tool_call_id"],
            [],
        ).append(event.get("delta", ""))
        out.append(
            _evt(
                CUSTOM,
                name=TOOL_RESULT_DELTA,
                value={
                    "toolCallId": event["tool_call_id"],
                    "delta": event.get("delta", ""),
                },
            ),
        )

    elif etype == "TOOL_RESULT_END":
        state.last_tool_result_state = event.get("state", "success")
        content = "".join(
            state.tool_result_buffers.pop(event["tool_call_id"], []),
        )
        out.append(
            _bounded_tool_result(_evt(
                TOOL_CALL_RESULT,
                toolCallId=event["tool_call_id"],
                messageId=event.get("reply_id"),
                content=content or str(event.get("state", "success")),
            )),
        )
        # AG-UI's ToolCallResult has content but no execution-state field.
        # Preserve success/error for the product timeline as an extension.
        out.append(
            _evt(
                CUSTOM,
                name=TOOL_RESULT_END,
                value={
                    "toolCallId": event["tool_call_id"],
                    "state": event.get("state"),
                },
            ),
        )
        out.append(
            _evt(
                CUSTOM,
                name=TASK_TIMELINE_EVENT,
                value={
                    "phase": "tool_result_end",
                    "toolCallId": event["tool_call_id"],
                    "state": event.get("state"),
                },
            ),
        )

    elif etype == "REQUIRE_USER_CONFIRM":
        tool_calls = event.get("tool_calls", [])
        out.append(
            _evt(
                CUSTOM,
                name=HITL_APPROVAL_REQUEST,
                value={
                    "toolCalls": tool_calls,
                    "decisionVersion": approval_request_version(
                        event.get("reply_id", ""), tool_calls,
                    ),
                },
            ),
        )

    elif etype == "TOOL_CONTENT_REFERENCE":
        ref = event["content_ref"]
        out.append(_evt(CUSTOM, name="TOOL_CONTENT_AVAILABLE", value={
            "toolCallId": event["tool_call_id"], "kind": ref["kind"],
            "sha256": ref["sha256"], "sizeBytes": ref["size_bytes"],
            "preview": ref["preview"], "incomplete": ref["incomplete"],
        }))

    elif etype == "CUSTOM":
        out.append(
            _evt(CUSTOM, name=event.get("name", ""), value=event.get("value", {})),
        )

    # Events with no AG-UI projection (DATA_BLOCK_*,
    # HINT_BLOCK, USER_*, REQUIRE_EXTERNAL_EXECUTION, ...) are dropped
    # from the browser stream; the event consumer still sees the raw
    # event for persistence.
    return out
