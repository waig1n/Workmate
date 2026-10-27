# -*- coding: utf-8 -*-
"""Translator unit tests - AgentScope event -> AG-UI mapping."""


from workmate_product.projections.translator import (
    HITL_APPROVAL_REQUEST,
    REASONING_MESSAGE_CONTENT,
    REASONING_MESSAGE_END,
    REASONING_MESSAGE_START,
    RUN_CANCELLED,
    RUN_ERROR,
    RUN_FINISHED,
    RUN_STARTED,
    STEP_FINISHED,
    STEP_STARTED,
    TASK_COMPLETED,
    TASK_TIMELINE_EVENT,
    TEXT_MESSAGE_CONTENT,
    TEXT_MESSAGE_END,
    TEXT_MESSAGE_START,
    TOOL_CALL_ARGS,
    TOOL_CALL_END,
    TOOL_CALL_RESULT,
    TOOL_CALL_START,
    TranslationState,
    translate_event,
)

TID = "conv-1"


def types(events):
    return [e["type"] for e in events]


def test_reply_start_maps_to_run_started():
    events = translate_event(
        {"type": "REPLY_START", "session_id": "s", "reply_id": "r1", "name": "a"},
        TID,
    )
    assert types(events) == [RUN_STARTED, "CUSTOM"]
    assert events[0] == {
        "type": RUN_STARTED, "threadId": TID, "runId": "r1",
    }
    assert events[1]["name"] == TASK_TIMELINE_EVENT
    assert events[1]["value"]["phase"] == "run_started"


def test_resume_reply_start_does_not_re_emit_run_started():
    """AgentScope 2.0.7 resumes a parked reply with a synthetic
    ReplyStartEvent carrying the same reply_id. AG-UI clients treat
    RUN_STARTED as a reset, so only the timeline event is re-emitted."""
    state = TranslationState()
    first = translate_event(
        {"type": "REPLY_START", "session_id": "s", "reply_id": "r1"},
        TID,
        state=state,
    )
    resumed = translate_event(
        {"type": "REPLY_START", "session_id": "s", "reply_id": "r1"},
        TID,
        state=state,
    )

    assert RUN_STARTED in types(first)
    assert RUN_STARTED not in types(resumed)
    assert resumed[0]["name"] == TASK_TIMELINE_EVENT
    assert resumed[0]["value"]["phase"] == "run_resumed"
    assert resumed[0]["value"]["replyId"] == "r1"

    # A different reply cannot replace a still-live/parked top-level Reply.
    foreign = translate_event(
        {"type": "REPLY_START", "session_id": "s", "reply_id": "r2"},
        TID,
        state=state,
    )
    assert foreign == []

    translate_event(
        {"type": "REPLY_END", "session_id": "s", "reply_id": "r1",
         "finished_reason": "completed"},
        TID,
        state=state,
    )
    # A genuinely new reply after the matching terminal event is accepted.
    nxt = translate_event(
        {"type": "REPLY_START", "session_id": "s", "reply_id": "r2-next"},
        TID,
        state=state,
    )
    assert RUN_STARTED in types(nxt)


def test_reply_end_completed():
    events = translate_event(
        {"type": "REPLY_END", "reply_id": "r1", "finished_reason": "completed"},
        TID,
    )
    assert types(events) == [RUN_FINISHED, "CUSTOM"]
    assert events[1]["name"] == TASK_COMPLETED
    assert events[1]["value"]["reason"] == "completed"


def test_completed_reply_after_terminal_tool_error_is_run_error():
    state = TranslationState()
    translate_event(
        {"type": "REPLY_START", "reply_id": "r-terminal-error"},
        TID,
        state,
    )
    translate_event(
        {
            "type": "TOOL_RESULT_END",
            "reply_id": "r-terminal-error",
            "tool_call_id": "write-1",
            "state": "error",
        },
        TID,
        state,
    )
    events = translate_event(
        {
            "type": "REPLY_END",
            "reply_id": "r-terminal-error",
            "finished_reason": "completed",
        },
        TID,
        state,
    )

    assert types(events) == [RUN_ERROR, "CUSTOM"]
    assert events[0]["code"] == "incomplete_tool_execution"
    assert events[1]["value"] == {
        "reason": "completed",
        "taskStatus": "failed",
        "completionGate": "terminal_tool_failure",
    }


def test_successful_retry_restores_completed_stream_status():
    state = TranslationState(last_tool_result_state="error")
    translate_event(
        {
            "type": "TOOL_RESULT_END", "reply_id": "r-retry",
            "tool_call_id": "write-2", "state": "success",
        },
        TID,
        state,
    )
    events = translate_event(
        {
            "type": "REPLY_END", "reply_id": "r-retry",
            "finished_reason": "completed",
        },
        TID,
        state,
    )
    assert types(events) == [RUN_FINISHED, "CUSTOM"]


def test_reply_end_error():
    events = translate_event(
        {
            "type": "REPLY_END", "reply_id": "r1",
            "finished_reason": "error",
            "error": {"type": "upstream", "message": "boom"},
        },
        TID,
    )
    assert types(events) == [RUN_ERROR, "CUSTOM"]
    assert events[0]["message"] == "boom"
    assert events[0]["code"] == "upstream"


def test_reply_end_interrupted_is_cancelled():
    for reason in ("interrupted", "exceed_max_iters"):
        events = translate_event(
            {"type": "REPLY_END", "reply_id": "r1", "finished_reason": reason},
            TID,
        )
        assert types(events) == [RUN_CANCELLED, "CUSTOM"]
        assert events[1]["value"]["reason"] == reason


def test_text_block_lifecycle():
    for raw, expected in [
        ({"type": "TEXT_BLOCK_START", "reply_id": "r1", "block_id": "b"},
         TEXT_MESSAGE_START),
        ({"type": "TEXT_BLOCK_DELTA", "reply_id": "r1", "block_id": "b",
          "delta": "hi"}, TEXT_MESSAGE_CONTENT),
        ({"type": "TEXT_BLOCK_END", "reply_id": "r1", "block_id": "b"},
         TEXT_MESSAGE_END),
    ]:
        events = translate_event(raw, TID)
        assert len(events) == 1
        assert events[0]["type"] == expected
    delta = translate_event(
        {"type": "TEXT_BLOCK_DELTA", "reply_id": "r1", "block_id": "b",
         "delta": "hi"}, TID,
    )[0]
    assert delta == {
        "type": TEXT_MESSAGE_CONTENT, "messageId": "b", "delta": "hi",
    }


def test_thinking_maps_to_standard_reasoning_lifecycle():
    cases = [
        ({"type": "THINKING_BLOCK_START", "reply_id": "r1", "block_id": "b"},
         REASONING_MESSAGE_START),
        ({"type": "THINKING_BLOCK_DELTA", "reply_id": "r1", "block_id": "b",
          "delta": "hmm"}, REASONING_MESSAGE_CONTENT),
        ({"type": "THINKING_BLOCK_END", "reply_id": "r1", "block_id": "b"},
         REASONING_MESSAGE_END),
    ]
    for raw, expected in cases:
        event = translate_event(raw, TID)[0]
        assert event["type"] == expected
        assert event["messageId"] == "b"


def test_tool_call_lifecycle():
    assert translate_event(
        {"type": "TOOL_CALL_START", "reply_id": "r1", "tool_call_id": "t1",
         "tool_call_name": "Write"}, TID,
    ) == [{
        "type": TOOL_CALL_START, "toolCallId": "t1", "toolCallName": "Write",
        "parentMessageId": "r1",
    }]
    assert translate_event(
        {"type": "TOOL_CALL_DELTA", "reply_id": "r1", "tool_call_id": "t1",
         "delta": '{"path"'}, TID,
    ) == [{
        "type": TOOL_CALL_ARGS, "toolCallId": "t1", "delta": '{"path"',
    }]
    events = translate_event(
        {"type": "TOOL_CALL_END", "reply_id": "r1", "tool_call_id": "t1"}, TID,
    )
    assert types(events) == [TOOL_CALL_END, "CUSTOM"]
    assert events[1]["name"] == TASK_TIMELINE_EVENT


def test_model_steps_and_tool_result_use_standard_events():
    state = TranslationState()
    assert translate_event(
        {"type": "MODEL_CALL_START", "reply_id": "r1", "model_name": "qwen"},
        TID,
        state,
    ) == [{"type": STEP_STARTED, "stepName": "qwen"}]
    end = translate_event(
        {"type": "MODEL_CALL_END", "reply_id": "r1", "input_tokens": 2,
         "output_tokens": 3},
        TID,
        state,
    )
    assert end[0] == {"type": STEP_FINISHED, "stepName": "qwen"}

    translate_event(
        {"type": "TOOL_CALL_START", "reply_id": "r1", "tool_call_id": "t1",
         "tool_call_name": "Read"}, TID, state,
    )
    translate_event(
        {"type": "TOOL_RESULT_TEXT_DELTA", "reply_id": "r1",
         "tool_call_id": "t1", "delta": "saved "},
        TID,
        state,
    )
    events = translate_event(
        {"type": "TOOL_RESULT_END", "reply_id": "r1", "tool_call_id": "t1",
         "state": "success"},
        TID,
        state,
    )
    assert events[0] == {
        "type": TOOL_CALL_RESULT,
        "toolCallId": "t1",
        "messageId": "r1",
        "content": "saved ",
    }
    assert events[1]["name"] == "TOOL_RESULT_END"
    assert events[1]["value"]["state"] == "success"
    assert events[2]["name"] == TASK_TIMELINE_EVENT


def test_require_user_confirm_maps_to_hitl_custom():
    events = translate_event(
        {"type": "REQUIRE_USER_CONFIRM",
         "tool_calls": [{"type": "tool_call", "id": "t1", "name": "Bash"}]},
        TID,
    )
    assert events[0]["type"] == "CUSTOM"
    assert events[0]["name"] == HITL_APPROVAL_REQUEST
    assert events[0]["value"]["toolCalls"][0]["id"] == "t1"


def test_unknown_events_dropped():
    assert translate_event({"type": "HINT_BLOCK"}, TID) == []
    assert translate_event({"type": "DATA_BLOCK_START"}, TID) == []


def test_run_error_events_carry_their_run_id():
    """Every translated RUN_ERROR is scoped to its reply: the frontend
    drops replayed error frames that belong to a different run, so the
    runId must survive translation (terminal tool failure and runtime
    reply error alike)."""
    state = TranslationState()

    tool_failure = translate_event({
        "type": "REPLY_END", "session_id": "s", "reply_id": "reply-a",
        "finished_reason": "completed",
    }, "c1", state=state)
    # Seed a failing terminal tool result for the completion gate.
    state2 = TranslationState()
    state2.active_reply_id = "reply-b"
    state2.last_tool_result_state = "error"
    gated = translate_event({
        "type": "REPLY_END", "session_id": "s", "reply_id": "reply-b",
        "finished_reason": "completed",
    }, "c1", state=state2)

    runtime_error = translate_event({
        "type": "REPLY_END", "session_id": "s", "reply_id": "reply-c",
        "finished_reason": "error", "error": {"message": "boom"},
    }, "c1", state=TranslationState())

    for events, reply_id in (
        (gated, "reply-b"),
        (runtime_error, "reply-c"),
    ):
        errors = [e for e in events if e["type"] == RUN_ERROR]
        assert errors, events
        assert errors[0]["runId"] == reply_id
