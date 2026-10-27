"""Directory cursor timestamps keep UTC semantics when bound to PostgreSQL."""
import base64
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.services.user_management import _decode_cursor, _encode_cursor


@pytest.mark.parametrize('timestamp', [
    '2026-09-20T01:02:03.456789',
    '2026-09-20T01:02:03.456789+00:00',
    '2026-09-20T09:02:03.456789+08:00',
])
def test_decoded_cursor_is_aware_utc_even_for_older_naive_tokens(timestamp):
    token = base64.urlsafe_b64encode(json.dumps({'c': timestamp, 'i': 'user'}).encode()).decode()
    created_at, user_id = _decode_cursor(token)
    assert created_at == datetime(2026, 9, 20, 1, 2, 3, 456789, tzinfo=timezone.utc)
    assert created_at.utcoffset() == timedelta(0)
    assert user_id == 'user'


def test_cursor_roundtrip_preserves_instant_and_tie_breaker():
    created_at = datetime(2026, 9, 20, 9, 2, 3, 456789,
                          tzinfo=timezone(timedelta(hours=8)))
    decoded, user_id = _decode_cursor(_encode_cursor(created_at, 'user-b'))
    assert decoded == created_at
    assert decoded.tzinfo == timezone.utc
    assert user_id == 'user-b'
