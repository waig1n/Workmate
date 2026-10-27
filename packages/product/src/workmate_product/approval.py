"""Pure shared identity for replayable Product permission requests."""
import hashlib
import json


def approval_request_version(reply_id: str, tool_calls: list[dict]) -> str:
    """Stable version attached to replayable HITL request events."""
    normalized = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "input": item.get("input"),
        }
        for item in tool_calls
    ]
    payload = json.dumps(
        {"reply_id": reply_id, "tool_calls": normalized},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "approval-" + hashlib.sha256(payload).hexdigest()[:32]
