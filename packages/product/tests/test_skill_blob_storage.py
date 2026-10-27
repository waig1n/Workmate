"""Immutable object writes and bounded, independently verified downloads."""
import hashlib
from io import BytesIO

import pytest

from workmate_product.skill_storage import S3SkillBlobStore, key_for_digest
from workmate_product.application.skill_artifacts import SkillArtifactError


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.writes = 0

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, **kwargs):
        from botocore.exceptions import ClientError
        assert IfNoneMatch == '*'
        if Key in self.objects:
            raise ClientError({'Error': {'Code': 'PreconditionFailed'},
                               'ResponseMetadata': {'HTTPStatusCode': 412}}, 'PutObject')
        self.objects[Key] = Body
        self.writes += 1

    def get_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        body = self.objects[Key]
        return {'ContentLength': len(body), 'Body': BytesIO(body)}


class MemoryBlobStore:
    """Explicit test dependency, never a production fallback."""
    def __init__(self):
        self.objects = {}

    async def put_verified(self, digest, raw):
        key = key_for_digest(digest)
        assert hashlib.sha256(raw).hexdigest() == digest
        previous = self.objects.setdefault(key, raw)
        if previous != raw:
            raise SkillArtifactError('Object digest mismatch')
        return key

    async def get_verified(self, digest, size_bytes):
        raw = self.objects.get(key_for_digest(digest))
        if raw is None or len(raw) != size_bytes or hashlib.sha256(raw).hexdigest() != digest:
            raise SkillArtifactError('Skill object integrity check failed')
        return raw


def store(client):
    return S3SkillBlobStore(endpoint='http://s3.test', bucket='skills', access_key='access',
                           secret_key='secret', client=client)


@pytest.mark.asyncio
async def test_upload_verifies_content_and_never_overwrites():
    client = FakeS3()
    blob = store(client)
    raw = b'archive'
    digest = hashlib.sha256(raw).hexdigest()
    key = await blob.put_verified(digest, raw)
    assert key == f'sha256/{digest[:2]}/{digest}.zip'
    assert await blob.put_verified(digest, raw) == key
    assert client.writes == 1
    assert await blob.get_verified(digest, len(raw)) == raw
    client.objects[key] = b'corrupt'
    with pytest.raises(SkillArtifactError):
        await blob.put_verified(digest, raw)
    assert client.objects[key] == b'corrupt'


@pytest.mark.asyncio
async def test_missing_size_and_digest_mismatch_fail_closed():
    client = FakeS3()
    blob = store(client)
    digest = hashlib.sha256(b'abc').hexdigest()
    with pytest.raises(SkillArtifactError):
        await blob.get_verified(digest, 3)
    with pytest.raises(SkillArtifactError):
        await blob.put_verified(digest, b'wrong')
    client.objects[key_for_digest(digest)] = b'abc'
    with pytest.raises(SkillArtifactError):
        await blob.get_verified(digest, 2)


@pytest.mark.parametrize('digest', ['', '../secret', 'g' * 64, 'A' * 64])
def test_only_canonical_digests_are_object_keys(digest):
    with pytest.raises(SkillArtifactError):
        key_for_digest(digest)


@pytest.mark.asyncio
async def test_prepared_publication_only_reuses_verified_request_bytes():
    from workmate_product.skill_storage import PreparedSkillBlobStore
    from test_skill_artifacts import archive
    delegate = MemoryBlobStore()
    prepared = PreparedSkillBlobStore(delegate)
    artifact = await prepared.prepare(archive())
    assert await prepared.put_verified(artifact.digest, artifact.archive) == key_for_digest(artifact.digest)
    assert await prepared.get_verified(artifact.digest, len(artifact.archive)) == artifact.archive
    with pytest.raises(SkillArtifactError):
        await prepared.get_verified(artifact.digest, 1)
    delegate.objects.clear()
    # Verification is scoped to the preparing request, never globally cached.
    with pytest.raises(SkillArtifactError):
        await PreparedSkillBlobStore(delegate).get_verified(artifact.digest, len(artifact.archive))


@pytest.mark.asyncio
async def test_failed_verification_cannot_prepare_transaction_store():
    from workmate_product.skill_storage import PreparedSkillBlobStore
    from test_skill_artifacts import archive
    class WrongKey(MemoryBlobStore):
        async def put_verified(self, digest, raw):
            return 'wrong-key'
    prepared = PreparedSkillBlobStore(WrongKey())
    with pytest.raises(SkillArtifactError, match='invalid key'):
        await prepared.prepare(archive())
    assert not prepared._verified


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [False, True])
async def test_object_listing_paginates_and_rejects_invalid_continuation(invalid):
    from datetime import datetime, timezone
    class Listing:
        def list_objects_v2(self, **kwargs):
            assert kwargs['Prefix'] == 'sha256/' and kwargs['MaxKeys'] == 1000
            if kwargs.get('ContinuationToken'):
                assert kwargs['ContinuationToken'] == 'next'
                return {'Contents': [], 'IsTruncated': False}
            return {'Contents': [{'Key': 'sha256/a', 'Size': 3, 'LastModified': datetime.now(timezone.utc)}],
                    'IsTruncated': True, 'NextContinuationToken': None if invalid else 'next'}
    if invalid:
        with pytest.raises(SkillArtifactError, match='continuation'):
            _ = [item async for item in store(Listing()).list_objects()]
    else:
        result = [item async for item in store(Listing()).list_objects()]
        assert len(result) == 1 and result[0].size_bytes == 3


@pytest.mark.asyncio
async def test_storage_configuration_and_sdk_errors_are_safe():
    from types import SimpleNamespace
    from workmate_product.skill_storage import get_skill_blob_store
    with pytest.raises(SkillArtifactError, match='not configured'):
        get_skill_blob_store(SimpleNamespace(info={}))
    blank = S3SkillBlobStore(endpoint='', bucket='skills', access_key='', secret_key='')
    with pytest.raises(SkillArtifactError, match='credentials'):
        await blank.get_verified('a' * 64, 1)
    class Failed:
        def put_object(self, **kwargs):
            from botocore.exceptions import ClientError
            raise ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'private-credential'}}, 'PutObject')
    raw = b'content'
    with pytest.raises(SkillArtifactError) as error:
        await store(Failed()).put_verified(hashlib.sha256(raw).hexdigest(), raw)
    assert 'private-credential' not in str(error.value)
