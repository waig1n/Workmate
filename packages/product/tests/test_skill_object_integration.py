"""Real S3-compatible service plus isolated PostgreSQL transactions."""
import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from test_skill_repository import skill_database
from test_execution_context import database
from test_skill_artifacts import archive
from workmate_product.application.skill_artifacts import SkillArtifactError, parse_skill_archive
from workmate_product.application.skill_repository import record_skill_artifact, load_skill_artifact
from workmate_product.skill_storage import S3SkillBlobStore, key_for_digest


@pytest_asyncio.fixture
async def object_database(skill_database):
    endpoint = os.getenv('WORKMATE_TEST_SKILL_S3_ENDPOINT')
    if not endpoint:
        pytest.skip('requires an isolated S3 test service')
    import boto3
    from botocore.config import Config
    bucket = 'skill-test-' + uuid.uuid4().hex
    client = boto3.client('s3', endpoint_url=endpoint,
        aws_access_key_id=os.environ['WORKMATE_TEST_SKILL_S3_ACCESS_KEY'],
        aws_secret_access_key=os.environ['WORKMATE_TEST_SKILL_S3_SECRET_KEY'],
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                      connect_timeout=5, read_timeout=5))
    client.create_bucket(Bucket=bucket)
    sessions, admin = skill_database
    store = S3SkillBlobStore(endpoint=endpoint, bucket=bucket, access_key='injected',
                            secret_key='injected', client=client)
    sessions.configure(info={'skill_blob_store': store})
    try:
        yield sessions, admin, store, client, bucket
    finally:
        for item in client.list_objects_v2(Bucket=bucket).get('Contents', []):
            client.delete_object(Bucket=bucket, Key=item['Key'])
        client.delete_bucket(Bucket=bucket)
        client.close()


@pytest.mark.asyncio
async def test_concurrent_publish_stores_only_metadata_and_restart_reads(object_database):
    sessions, admin, store, client, bucket = object_database
    raw = archive()
    async def publish():
        async with sessions.begin() as db:
            return await record_skill_artifact(db, raw)
    results = await asyncio.gather(*(publish() for _ in range(4)))
    assert len({result.digest for result in results}) == 1
    row = await admin.fetchrow('SELECT storage_key,size_bytes FROM skill_artifacts')
    assert row['storage_key'] == key_for_digest(results[0].digest)
    assert row['size_bytes'] == len(raw)
    assert len([item async for item in store.list_objects()]) == 1
    # New store/session proves no process-local byte registry is needed.
    sessions.configure(info={'skill_blob_store': S3SkillBlobStore(
        endpoint=os.environ['WORKMATE_TEST_SKILL_S3_ENDPOINT'], bucket=bucket,
        access_key=os.environ['WORKMATE_TEST_SKILL_S3_ACCESS_KEY'],
        secret_key=os.environ['WORKMATE_TEST_SKILL_S3_SECRET_KEY'])})
    async with sessions.begin() as db:
        assert (await load_skill_artifact(db, results[0].digest)).archive == raw


@pytest.mark.asyncio
async def test_database_rollback_leaves_only_reportable_orphan(object_database):
    sessions, admin, store, client, bucket = object_database
    with pytest.raises(RuntimeError, match='rollback'):
        async with sessions.begin() as db:
            await record_skill_artifact(db, archive())
            raise RuntimeError('rollback')
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts') == 0
    assert await admin.fetchval('SELECT count(*) FROM capability_skill_artifacts') == 0
    assert len([item async for item in store.list_objects()]) == 1


@pytest.mark.asyncio
async def test_missing_or_tampered_object_never_overwritten_or_published(object_database):
    sessions, admin, store, client, bucket = object_database
    artifact = parse_skill_archive(archive())
    client.put_object(Bucket=bucket, Key=key_for_digest(artifact.digest), Body=b'tampered')
    with pytest.raises(SkillArtifactError):
        async with sessions.begin() as db:
            await record_skill_artifact(db, artifact.archive)
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts') == 0
    assert client.get_object(Bucket=bucket, Key=key_for_digest(artifact.digest))['Body'].read() == b'tampered'
    client.delete_object(Bucket=bucket, Key=key_for_digest(artifact.digest))
    async with sessions.begin() as db:
        await record_skill_artifact(db, artifact.archive)
    client.delete_object(Bucket=bucket, Key=key_for_digest(artifact.digest))
    with pytest.raises(SkillArtifactError):
        async with sessions.begin() as db:
            await load_skill_artifact(db, artifact.digest)
