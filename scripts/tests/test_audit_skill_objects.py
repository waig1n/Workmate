"""Object audit preserves live/history references and never mutates storage."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from audit_skill_objects import AuditError, classify_objects, orphan_report, parser


def test_reference_grace_and_unknown_age_are_protected():
    now = datetime.now(timezone.utc)
    objects = [SimpleNamespace(key=key, size_bytes=1, last_modified=modified) for key, modified in (
        ('referenced', now - timedelta(days=99)), ('old', now - timedelta(days=99)),
        ('new', now - timedelta(days=1)), ('unknown', None),
        ('naive', (now - timedelta(days=1)).replace(tzinfo=None)),
    )]
    rows = classify_objects(objects, {'referenced'}, now=now, grace_days=7)
    assert {row['key']: row['status'] for row in rows} == {
        'referenced': 'referenced', 'old': 'orphan_candidate', 'new': 'within_grace',
        'unknown': 'age_unknown', 'naive': 'within_grace',
    }
    with pytest.raises(AuditError, match='positive'):
        classify_objects(objects, set(), now=now, grace_days=0)


def test_cli_has_no_apply_or_migration_modes():
    assert parser().parse_args([]).grace_days == 7
    assert parser().parse_args(['--check']).check
    for argument in ('--apply', 'bytea', 'legacy'):
        with pytest.raises(SystemExit):
            parser().parse_args([argument])


@pytest.mark.asyncio
async def test_report_protects_metadata_bindings_and_history_without_writes():
    from workmate_product.skill_storage import key_for_digest
    digests = ['a' * 64, 'b' * 64, 'c' * 64, 'd' * 64]
    class Result:
        def __init__(self, rows): self.rows = rows
        def all(self): return self.rows
        def scalars(self): return self.rows
    class DB:
        async def execute(self, sql):
            query = str(sql)
            assert query.startswith('SELECT ')
            if 'FROM skill_artifacts' in query:
                return Result([(digests[0], key_for_digest(digests[0]))])
            if 'FROM capability_skill_artifacts' in query:
                return Result([digests[1]])
            assert 'FROM runtime_execution_specs' in query
            return Result([{'skill_artifacts': [{'digest': digests[2]}]}])
    class Store:
        async def list_objects(self):
            for digest in digests:
                yield SimpleNamespace(key=key_for_digest(digest), size_bytes=1,
                    last_modified=datetime.now(timezone.utc) - timedelta(days=30))
    rows = await orphan_report(DB(), Store(), 7)
    assert [row['status'] for row in rows] == ['referenced'] * 3 + ['orphan_candidate']
