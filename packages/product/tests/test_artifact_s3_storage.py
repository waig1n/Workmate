import hashlib
import io

import pytest
from botocore.exceptions import ClientError
from workmate_product.artifact_storage import S3ArtifactStore
from workmate_product.application.artifact_objects import ArtifactObjectUnavailable


class Client:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, Metadata, IfNoneMatch):
        assert IfNoneMatch == '*'
        if Key in self.objects:
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        self.objects[Key] = (Body, Metadata)

    def get_object(self, *, Bucket, Key):
        content, metadata = self.objects[Key]
        return {'Body': io.BytesIO(content), 'ContentLength': len(content), 'Metadata': metadata}


def test_publish_idempotent_and_reject_corruption():
    client = Client()
    store = S3ArtifactStore(client, 'private-product')
    content = b'final report'
    digest = hashlib.sha256(content).hexdigest()
    key = f'.objects/sha256/{digest[:2]}/{digest}'
    assert store.publish(key, content) == digest
    assert store.publish(key, content) == digest
    client.objects[key] = (b'corrupt data', {'sha256': digest})
    with pytest.raises(ArtifactObjectUnavailable):
        store.publish(key, content)


def test_attachment_snapshot_migrated_metadata_and_private_cleanup():
    store = S3ArtifactStore(Client(), 'private-product')
    key = '.inputs/conversation/task/report.txt'
    store.publish(key, b'hello')
    snapshot = store.snapshot(key, None, 5)
    assert snapshot.path.read_bytes() == b'hello'
    snapshot.close()
    assert not snapshot.path.exists()
    with pytest.raises(ArtifactObjectUnavailable):
        store.snapshot(key, None, 4)


@pytest.mark.parametrize('key', ['../escape', '.inputs/c/../secret', '/absolute', '.inputs/c/\\x'])
def test_invalid_keys(key):
    with pytest.raises(ValueError):
        S3ArtifactStore(Client(), 'private-product').publish(key, b'no')


@pytest.mark.parametrize('missing', ['artifact_s3_endpoint','artifact_s3_bucket','artifact_s3_access_key','artifact_s3_secret_key'])
def test_s3_requires_explicit_credentials_before_sdk_lookup(missing):
    from types import SimpleNamespace
    from workmate_product.artifact_storage import artifact_store_from_settings
    values = dict(artifact_s3_endpoint='http://objects:9000',artifact_s3_bucket='private',
        artifact_s3_access_key='access',artifact_s3_secret_key='secret',
        artifact_storage=SimpleNamespace(backend='s3',region='us-east-1',timeout_seconds=5))
    values[missing] = ''
    with pytest.raises(ValueError,match='explicit'):
        artifact_store_from_settings(SimpleNamespace(**values))


def test_snapshot_rejects_digest_that_does_not_match_immutable_key():
    digest = 'a'*64
    with pytest.raises(ValueError):
        S3ArtifactStore(Client(),'private').snapshot(f'.objects/sha256/aa/{digest}','b'*64,0)


def test_existing_attachment_without_metadata_cannot_pass_migration_publish():
    client = Client()
    key = '.inputs/conversation/task/file.txt'
    client.objects[key] = (b'content',{})
    with pytest.raises(ArtifactObjectUnavailable):
        S3ArtifactStore(client,'private').publish(key,b'content')


def _publish(store, method, content=b'final report'):
    digest = hashlib.sha256(content).hexdigest()
    key = f'.objects/sha256/{digest[:2]}/{digest}'
    if method == 'publish_file':
        return store.publish_file(key, io.BytesIO(content), digest, len(content))
    return store.publish(key, content, digest)


@pytest.mark.parametrize('method', ['publish', 'publish_file'])
@pytest.mark.parametrize('response', [None, [], 'invalid', {}, {'Error': None},
    {'Error': []}, {'Error': 'invalid'}, {'Error': {}}, {'Error': {'Code': []}},
    {'Error': {'Code': 'AccessDenied'}}])
def test_publication_normalizes_malformed_sdk_errors_and_preserves_cause(method, response):
    error = RuntimeError('storage unavailable')
    error.response = response

    class FailingClient:
        def put_object(self, **kwargs):
            raise error

        def get_object(self, **kwargs):
            pytest.fail('Non-conflict failures must not verify or accept an existing object')

    with pytest.raises(ArtifactObjectUnavailable, match='publication failed') as caught:
        _publish(S3ArtifactStore(FailingClient(), 'private'), method)
    assert caught.value.__cause__ is error


@pytest.mark.parametrize('method', ['publish', 'publish_file'])
def test_publication_normalizes_real_sdk_read_timeout(method):
    from botocore.exceptions import ReadTimeoutError
    error = ReadTimeoutError(endpoint_url='http://test-storage.invalid')
    assert error.response is None

    class FailingClient:
        def put_object(self, **kwargs):
            raise error

    with pytest.raises(ArtifactObjectUnavailable) as caught:
        _publish(S3ArtifactStore(FailingClient(), 'private'), method)
    assert caught.value.__cause__ is error


@pytest.mark.parametrize('method', ['publish', 'publish_file'])
@pytest.mark.parametrize('code', ['PreconditionFailed', '412', 'ConditionalRequestConflict', '409'])
@pytest.mark.parametrize('corrupt', [False, True])
def test_publication_conflicts_require_full_byte_verification(method, code, corrupt):
    content = b'final report'
    digest = hashlib.sha256(content).hexdigest()

    class ConflictClient:
        def put_object(self, **kwargs):
            raise ClientError({'Error': {'Code': code}}, 'PutObject')

        def get_object(self, **kwargs):
            stored = b'wrong report' if corrupt else content
            return {'Body': io.BytesIO(stored), 'ContentLength': len(stored),
                    'Metadata': {'sha256': digest}}

    store = S3ArtifactStore(ConflictClient(), 'private')
    if corrupt:
        with pytest.raises(ArtifactObjectUnavailable, match='integrity'):
            _publish(store, method, content)
    else:
        assert _publish(store, method, content) == digest


@pytest.mark.parametrize('method', ['publish', 'publish_file'])
def test_publication_does_not_translate_cancellation(method):
    from asyncio import CancelledError
    cancellation = CancelledError()

    class CancelledClient:
        def put_object(self, **kwargs):
            raise cancellation

    with pytest.raises(CancelledError) as caught:
        _publish(S3ArtifactStore(CancelledClient(), 'private'), method)
    assert caught.value is cancellation


def test_publish_file_streams_verified_bytes_and_closes_snapshot():
    class StreamingClient(Client):
        def put_object(self, *, Body, ContentLength, **kwargs):
            content = Body.read()
            assert len(content) == ContentLength
            return super().put_object(Body=content, **kwargs)

    client = StreamingClient()
    digest = _publish(S3ArtifactStore(client, 'private'), 'publish_file')
    assert client.objects[f'.objects/sha256/{digest[:2]}/{digest}'][0] == b'final report'


@pytest.mark.parametrize('size', [-1, True, 13])
def test_publish_file_rejects_invalid_or_mismatched_size_before_upload(size):
    content = b'final report'
    digest = hashlib.sha256(content).hexdigest()
    client = Client()
    with pytest.raises(ValueError):
        S3ArtifactStore(client, 'private').publish_file(
            f'.objects/sha256/{digest[:2]}/{digest}', io.BytesIO(content), digest, size)
    assert client.objects == {}


def test_snapshot_failed_stream_removes_partial_spool_and_closes_body(monkeypatch, tmp_path):
    import tempfile
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))

    class BrokenBody(io.BytesIO):
        def read(self, size):
            if self.tell():
                raise OSError('connection lost')
            return super().read(1)

    body = BrokenBody(b'final report')
    content = body.getvalue()
    digest = hashlib.sha256(content).hexdigest()

    class BrokenClient:
        def get_object(self, **kwargs):
            return {'Body': body, 'ContentLength': len(content), 'Metadata': {'sha256': digest}}

    with pytest.raises(ArtifactObjectUnavailable) as caught:
        S3ArtifactStore(BrokenClient(), 'private').snapshot(
            f'.objects/sha256/{digest[:2]}/{digest}', digest, len(content))
    assert isinstance(caught.value.__cause__, OSError)
    assert body.closed
    assert list(tmp_path.iterdir()) == []


def test_delete_inputs_removes_only_listed_conversation_objects():
    deleted = []

    class DeleteClient:
        def get_paginator(self, operation):
            assert operation == 'list_objects_v2'
            return self

        def paginate(self, **kwargs):
            assert kwargs == {'Bucket': 'private', 'Prefix': '.inputs/conversation/'}
            return [{}, {'Contents': [{'Key': '.inputs/conversation/task/report.txt'}]}]

        def delete_object(self, **kwargs):
            deleted.append(kwargs)

    store = S3ArtifactStore(DeleteClient(), 'private')
    store.delete_inputs('conversation')
    assert deleted == [{'Bucket': 'private', 'Key': '.inputs/conversation/task/report.txt'}]
    with pytest.raises(ValueError):
        store.delete_inputs('../other')


@pytest.mark.parametrize(('digest', 'size'), [(None, -1), (None, True), ('invalid', 0)])
def test_snapshot_rejects_invalid_descriptor_before_storage_access(digest, size):
    with pytest.raises(ValueError):
        S3ArtifactStore(Client(), 'private').snapshot('.inputs/conversation/task/file', digest, size)
