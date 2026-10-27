"""Lossless compaction of ready tool deltas; no timer or deferred persistence.

Identity and semantic metadata must match. The combined record retains the
first Core id/timestamp; constituent token ids do not grant execution authority. Boundaries and unrelated events
are never reordered. The limit is a record-size budget, not an input truncation.
"""

MAX_COMBINED_BYTES = 32 * 1024
TOOL_DELTAS = frozenset({'TOOL_CALL_DELTA', 'TOOL_RESULT_TEXT_DELTA'})


def coalesce_tool_deltas(events):
    compact = []
    previous_metadata = None
    previous_bytes = 0
    for event in events:
        value = event.get('delta')
        eligible = event.get('type') in TOOL_DELTAS and isinstance(value, str)
        metadata = {key: val for key, val in event.items() if key not in {'delta', 'id', 'created_at'}} if eligible else None
        size = len(value.encode('utf-8')) if eligible else 0
        if (eligible and compact and previous_metadata == metadata
                and previous_bytes + size <= MAX_COMBINED_BYTES):
            compact[-1] = {**compact[-1], 'delta': compact[-1]['delta'] + value}
            previous_bytes += size
        else:
            compact.append(event)
            previous_bytes = size
        previous_metadata = metadata
    return compact
