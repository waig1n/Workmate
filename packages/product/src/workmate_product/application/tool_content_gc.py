#!/usr/bin/env python3
"""Tool object inventory and explicitly offline, bounded orphan cleanup.

Apply requires every publisher to stay stopped for the entire maintenance
window, and this bucket to belong to this database only. Runtime observations
and the shared storage lock are additional guards, not an online publish fence.
"""
from datetime import datetime, timedelta, timezone
import asyncio
from workmate_product.persistence.repositories import tool_content_retention as retention
from workmate_product.persistence.repositories.tool_content import referenced_keys
from workmate_product.artifact_storage import validate_key
from workmate_product.application.tool_content_retention import bounds


def classify(objects, referenced, *, now=None, grace_days=7):
    if grace_days < 1:
        raise ValueError('Grace period must be positive')
    now = now or datetime.now(timezone.utc)
    result = []
    for item in objects:
        key = item['Key']
        if not key.startswith('.tool-content/sha256/'):
            continue
        modified = item.get('LastModified')
        status = ('referenced' if key in referenced else 'age_unknown' if modified is None
                  else 'within_grace' if modified > now - timedelta(days=grace_days)
                  else 'orphan_candidate')
        result.append(dict(key=key, size_bytes=item['Size'], status=status))
    return result


async def report(settings, sessions, *, grace_days=7):
    import asyncio
    from workmate_product.artifact_storage import tool_content_store_from_settings
    from workmate_product.persistence.repositories.tool_content import referenced_keys
    store = tool_content_store_from_settings(settings)
    if store is None:
        raise ValueError('Tool content inventory requires S3 storage')
    async with sessions() as db:
        referenced = await referenced_keys(db)
    def inventory():
        return [item for page in store.client.get_paginator('list_objects_v2').paginate(
            Bucket=store.bucket, Prefix='.tool-content/sha256/') for item in page.get('Contents', [])]
    return classify(await asyncio.to_thread(inventory), referenced, grace_days=grace_days)


async def _storage_io(call, **kwargs):
    """Keep the maintenance exclusion until an in-flight SDK call really exits."""
    operation=asyncio.create_task(asyncio.to_thread(call,**kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            try: await asyncio.shield(operation)
            except asyncio.CancelledError: pass
            except Exception: break
        if not operation.cancelled(): operation.exception()
        raise


async def _no_worker(redis, namespace):
    import re
    if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', namespace) is None:
        raise ValueError('Invalid coordination namespace')
    # Any live health key, even malformed or draining, blocks physical deletion.
    async for _ in redis.scan_iter(match=f'workmate:{namespace}:worker:*:health', count=100):
        raise ValueError('Worker heartbeat exists; stop all publishers before object deletion')


async def delete_candidates(sessions, store, redis, *, namespace, candidates,
                            writers_stopped=False, grace_days=7, limit=100):
    bounds(grace_days,limit)
    if not writers_stopped:
        raise ValueError('Explicit --writers-stopped maintenance confirmation is required')
    if len(candidates)>limit:
        raise ValueError('Candidate count exceeds one bounded batch')
    deleted=[]
    async with sessions.begin() as db:
        await retention.lock_object_gc(db)
        await retention.assert_no_writers(db)
        await _no_worker(redis,namespace)
        for candidate in candidates:
            key=validate_key(candidate['key'])
            if not key.startswith('.tool-content/sha256/'):
                raise ValueError('Only dedicated tool content objects may be deleted')
            # Re-read facts after acquiring maintenance exclusion, and again
            # before each deletion. No cached inventory reference grants delete.
            if key in await referenced_keys(db): continue
            await retention.assert_no_writers(db)
            await _no_worker(redis,namespace)
            try:
                head=await _storage_io(store.client.head_object,Bucket=store.bucket,Key=key)
            except Exception as error:
                code=getattr(error,'response',{}).get('Error',{}).get('Code')
                if code in {'404','NoSuchKey','NotFound'}: continue
                raise
            modified=head.get('LastModified')
            if modified is None or modified>datetime.now(timezone.utc)-timedelta(days=grace_days): continue
            if key in await referenced_keys(db): continue
            await _storage_io(store.client.delete_object,Bucket=store.bucket,Key=key)
            deleted.append(key)
    return deleted
