"""Compact browser projection; full retrieval facts remain in task_events."""
from dataclasses import dataclass, field

KNOWLEDGE_CUSTOM_EVENTS = {"knowledge_sources", "knowledge_retrieval"}
SOURCE_LIMIT = 100


def compact_sources(sources):
    result = {}
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, dict) or not isinstance(source.get("source_id"), str):
            continue
        item = {key: source[key] for key in (
            "source_id", "knowledge_base_id", "document_id", "filename", "chunk_index",
        ) if key in source}
        metadata = source.get("metadata") or {}
        item["metadata"] = {key: metadata[key] for key in (
            "page", "page_number", "slide", "sheet",
        ) if isinstance(metadata, dict) and key in metadata}
        result[item["source_id"]] = item
        if len(result) >= SOURCE_LIMIT:
            break
    return list(result.values())


def compact_event_payload(event_type, payload):
    if event_type == "knowledge_sources":
        return {"sources": compact_sources(payload.get("sources")), "details_available": True}
    if event_type == "knowledge_retrieval" or (
        event_type in {"tool_call", "tool_result"}
        and payload.get("tool", payload.get("tool_call_name", payload.get("toolCallName"))) == "search_knowledge"
    ):
        return {key: payload[key] for key in (
            "toolCallId", "status", "tool", "phase", "source_count",
        ) if key in payload}
    return payload


@dataclass
class KnowledgeProjection:
    tool_ids: set[str] = field(default_factory=set)
    finished_ids: set[str] = field(default_factory=set)
    sources: dict = field(default_factory=dict)
    started: bool = False
    count: int = 0
    failures: int = 0

    def status(self, reply_id, status):
        return {"type": "CUSTOM", "name": "knowledge_status", "runId": reply_id,
                "value": {"status": status, "search_count": self.count,
                          "failure_count": self.failures}}

    def start(self, reply_id):
        if self.started:
            return []
        self.started = True
        return [self.status(reply_id, "running")]

    def finish(self, reply_id, identity, status):
        if identity in self.finished_ids:
            return []
        self.finished_ids.add(identity)
        self.count += 1
        if status not in {"success", "completed"}:
            self.failures += 1
            if self.failures == 1:
                return [self.status(reply_id, "error")]
        return []

    def end(self, reply_id, reason):
        if not self.started:
            return []
        status = "error" if self.failures else "completed"
        if reason != "completed" and not self.failures:
            status = "interrupted"
        return [self.status(reply_id, status), {
            "type": "CUSTOM", "name": "knowledge_sources", "runId": reply_id,
            "value": {"sources": list(self.sources.values()), "details_available": True},
        }]
