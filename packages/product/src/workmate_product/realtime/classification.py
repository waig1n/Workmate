"""Public event semantics shared by durable projection and realtime delivery.

Unknown extensions remain durable until explicitly classified. This is not a
prefix-based allowlist: a new approval or business notification must not vanish
because its name has not yet been added here.

Only assistant body/reasoning has a separate Writer-owned delivery path. Tool
argument/result progress and model steps retain Product facts and reliable
publication so a Streams-only browser keeps its existing Activity behavior.
"""
_EPHEMERAL_TYPES = frozenset({
    'TEXT_MESSAGE_START', 'TEXT_MESSAGE_CONTENT', 'TEXT_MESSAGE_END',
    'REASONING_MESSAGE_START', 'REASONING_MESSAGE_CONTENT', 'REASONING_MESSAGE_END',
})
_EPHEMERAL_CUSTOM = frozenset({'REASONING_DELTA'})


def is_business_event(event_type: str, payload: dict) -> bool:
    if not isinstance(event_type, str) or not event_type or not isinstance(payload, dict):
        raise ValueError('Public event type and payload are required')
    if payload.get('type') != event_type:
        raise ValueError('Public event type disagrees with payload')
    if event_type == 'CUSTOM':
        name = payload.get('name')
        if name is not None and not isinstance(name, str):
            raise ValueError('Custom event semantic name must be a string or absent')
        return name not in _EPHEMERAL_CUSTOM
    return event_type not in _EPHEMERAL_TYPES
