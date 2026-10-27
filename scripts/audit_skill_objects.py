#!/usr/bin/env python3
"""Read-only Skill object inventory; never upload or delete objects.

Run with backend dependencies and PYTHONPATH=backend:packages/product/src:packages/contracts/src.
Database arguments name environment variables, never secret DSNs.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import sys


class AuditError(ValueError):
    pass


def classify_objects(objects, referenced, *, now, grace_days):
    if grace_days < 1:
        raise AuditError('grace_days_must_be_positive')
    rows = []
    for obj in objects:
        modified = obj.last_modified
        if modified is not None and modified.tzinfo is None:
            modified = modified.replace(tzinfo=timezone.utc)
        status = ('referenced' if obj.key in referenced else 'age_unknown' if modified is None
                  else 'within_grace' if modified > now - timedelta(days=grace_days) else 'orphan_candidate')
        rows.append({'key': obj.key, 'size_bytes': obj.size_bytes, 'status': status})
    return rows

async def orphan_report(db, store, grace_days):
    from sqlalchemy import text
    from workmate_product.skill_storage import key_for_digest
    referenced = set()
    for digest, key in (await db.execute(text('SELECT digest,storage_key FROM skill_artifacts'))).all():
        referenced.add(key_for_digest(digest))
        if key:
            referenced.add(key)
    for digest in (await db.execute(text('SELECT artifact_digest FROM capability_skill_artifacts'))).scalars():
        referenced.add(key_for_digest(digest))
    for payload in (await db.execute(text('SELECT payload FROM runtime_execution_specs'))).scalars():
        for ref in payload.get('skill_artifacts', []):
            referenced.add(key_for_digest(ref['digest']))
    objects = [obj async for obj in store.list_objects()]
    return classify_objects(objects, referenced, now=datetime.now(timezone.utc), grace_days=grace_days)

def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--database-env', default='DATABASE_URL', help='DB URL environment variable name')
    result.add_argument('--check', action='store_true', help='Read-only; also the default')
    result.add_argument('--grace-days', type=int, default=7)
    return result


def db_url(env_name):
    value = os.environ.get(env_name, '')
    if not value:
        raise AuditError('database_environment_missing')
    if value.startswith('postgresql://'):
        value = value.replace('postgresql://', 'postgresql+asyncpg://', 1)
    if not value.startswith('postgresql+asyncpg://'):
        raise AuditError('postgresql_database_required')
    return value


def configured_store():
    from app.config import Settings
    from workmate_product.skill_storage import store_from_settings
    keys = ['SKILL_S3_ENDPOINT', 'SKILL_S3_BUCKET', 'SKILL_S3_ACCESS_KEY', 'SKILL_S3_SECRET_KEY']
    values = [os.environ.get(key, '') for key in keys]
    if not all(values):
        raise AuditError('skill_blob_environment_missing')
    return store_from_settings(Settings(_env_file=None))

async def run(args):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    if args.grace_days < 1:
        raise AuditError('grace_days_must_be_positive')
    engine = create_async_engine(db_url(args.database_env))
    try:
        async with async_sessionmaker(engine)() as db, db.begin():
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            rows = await orphan_report(db, configured_store(), args.grace_days)
        return {'mode': 'check', 'rows': rows}
    finally:
        await engine.dispose()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        # Provider/driver exceptions may contain credentials. Only expose safe codes.
        print(json.dumps({'error': str(error) if isinstance(error, AuditError) else 'audit_failed',
                          'error_type': type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
