"""Private immutable Product files shared by Gateway and storage nodes.

Local mode deliberately keeps the existing filesystem implementation. S3 mode
never falls back to local bytes: an incomplete migration fails closed.
"""
import hashlib
import re
from pathlib import Path
import tempfile
from collections import OrderedDict
from collections.abc import Mapping
from threading import Lock

from .application.artifact_objects import ArtifactSnapshot, ArtifactObjectUnavailable


def _s3_error_code(error):
    """Transport errors can carry response=None instead of an S3 error body."""
    response = getattr(error, 'response', None)
    if not isinstance(response, Mapping):
        return None
    details = response.get('Error')
    if not isinstance(details, Mapping):
        return None
    code = details.get('Code')
    return code if isinstance(code, str) else None


def validate_key(key):
    if not isinstance(key, str) or not key or '\\' in key:
        raise ValueError('Invalid Product object key')
    parts = key.split('/')
    if any(part in {'', '.', '..'} or any(ord(c) < 32 for c in part) for part in parts):
        raise ValueError('Invalid Product object key')
    if parts[0] == '.objects':
        if re.fullmatch(r'\.objects/sha256/([0-9a-f]{2})/([0-9a-f]{64})', key) is None or parts[2] != parts[3][:2]:
            raise ValueError('Invalid immutable Product object key')
    elif parts[0] == '.tool-content':
        if re.fullmatch(r'\.tool-content/sha256/([0-9a-f]{2})/([0-9a-f]{64})', key) is None or parts[2] != parts[3][:2]:
            raise ValueError('Invalid immutable tool content key')
    elif parts[0] == '.inputs':
        if len(parts) != 4 or any(re.fullmatch('[A-Za-z0-9_-]{1,128}', part) is None for part in parts[1:3]):
            raise ValueError('Invalid Product attachment key')
    else:
        raise ValueError('Invalid Product object namespace')
    return key


class S3ArtifactStore:
    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    def snapshot(self, key, digest, size):
        validate_key(key)
        if type(size) is not int or size < 0:
            raise ValueError('Invalid Product object size')
        if digest is not None and re.fullmatch('[0-9a-f]{64}', digest) is None:
            raise ValueError('Invalid Product object checksum')
        if key.startswith(('.objects/', '.tool-content/')) and digest != key.rsplit('/',1)[1]:
            raise ValueError('Immutable key differs from checksum')
        snapshot = None
        body = None
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response['Body']
            metadata_digest = response.get('Metadata', {}).get('sha256')
            expected = digest or metadata_digest
            if key.startswith('.inputs/') and metadata_digest != expected:
                raise ArtifactObjectUnavailable('Attachment checksum metadata is unavailable')
            if not expected or response['ContentLength'] != size:
                raise ArtifactObjectUnavailable('Product object unavailable')
            checksum, count = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(prefix='workmate-artifact-', delete=False) as output:
                snapshot = ArtifactSnapshot(Path(output.name))
                while chunk := body.read(min(1024 * 1024, size + 1 - count)):
                    count += len(chunk)
                    if count > size:
                        raise ArtifactObjectUnavailable('Product object exceeds declared size')
                    checksum.update(chunk)
                    output.write(chunk)
                if count != size or checksum.hexdigest() != expected:
                    raise ArtifactObjectUnavailable('Product object integrity check failed')
            return snapshot
        except BaseException as error:
            if snapshot is not None:
                snapshot.close()
            if isinstance(error, Exception) and not isinstance(error, (ValueError, ArtifactObjectUnavailable)):
                raise ArtifactObjectUnavailable('Product object unavailable') from error
            raise
        finally:
            if body is not None:
                body.close()

    def publish(self, key, content, digest=None):
        validate_key(key)
        actual = hashlib.sha256(content).hexdigest()
        if key.startswith(('.objects/', '.tool-content/')) and key.rsplit('/',1)[1] != actual:
            raise ValueError('Immutable key differs from bytes')
        if digest is not None and actual != digest:
            raise ValueError('Product object checksum differs from bytes')
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=content,
                Metadata={'sha256': actual}, IfNoneMatch='*')
        except Exception as error:
            # An already present object is accepted only after full verification.
            code = _s3_error_code(error)
            if code not in {'PreconditionFailed', '412', 'ConditionalRequestConflict', '409'}:
                raise ArtifactObjectUnavailable('Product object publication failed') from error
        snapshot = self.snapshot(key, actual, len(content))
        snapshot.close()
        return actual

    def publish_file(self, key, source, digest, size):
        """Publish a seekable private spool without materializing its body in RAM."""
        validate_key(key)
        if (not key.startswith(('.tool-content/', '.objects/')) or key.rsplit('/', 1)[1] != digest
                or type(size) is not int or size < 0):
            raise ValueError('Invalid tool content descriptor')
        source.seek(0)
        checksum, count = hashlib.sha256(), 0
        while chunk := source.read(1024 * 1024):
            count += len(chunk)
            checksum.update(chunk)
        if count != size or checksum.hexdigest() != digest:
            raise ValueError('Tool content checksum differs from bytes')
        source.seek(0)
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=source,
                ContentLength=size, Metadata={'sha256': digest}, IfNoneMatch='*')
        except Exception as error:
            code = _s3_error_code(error)
            if code not in {'PreconditionFailed', '412', 'ConditionalRequestConflict', '409'}:
                raise ArtifactObjectUnavailable('Tool content publication failed') from error
        snapshot = self.snapshot(key, digest, size)
        snapshot.close()
        return digest

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=validate_key(key))

    def delete_inputs(self, conversation_id):
        if re.fullmatch('[A-Za-z0-9_-]{1,128}', conversation_id) is None:
            raise ValueError('Invalid conversation identity')
        prefix = '.inputs/' + conversation_id + '/'
        for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get('Contents', []):
                self.delete(item['Key'])


_stores = OrderedDict()
_stores_lock = Lock()


def artifact_store_from_settings(settings):
    policy = getattr(settings, 'artifact_storage', None)
    if policy is None or policy.backend == 'local':
        return None
    values = tuple(getattr(settings,name,None) for name in (
        'artifact_s3_endpoint','artifact_s3_bucket','artifact_s3_access_key','artifact_s3_secret_key'))
    if any(not isinstance(value,str) or not value.strip() for value in values):
        raise ValueError('S3 Product storage requires an explicit endpoint, bucket and credentials')
    endpoint,bucket,access,secret = values
    if not endpoint.startswith(('http://','https://')):
        raise ValueError('S3 Product endpoint must use HTTP or HTTPS')
    key = (*values,policy.region,policy.timeout_seconds)
    # Settings are process-static in production; bound test/reload variants too.
    with _stores_lock:
        if key in _stores:
            _stores.move_to_end(key)
            return _stores[key]
        import boto3
        from botocore.config import Config
        store = S3ArtifactStore(boto3.session.Session().client('s3',
            endpoint_url=endpoint, aws_access_key_id=access, aws_secret_access_key=secret,
            region_name=policy.region,
            config=Config(signature_version='s3v4', connect_timeout=policy.timeout_seconds,
                read_timeout=policy.timeout_seconds, retries={'max_attempts': 2},
                s3={'addressing_style': 'path'})), bucket)
        _stores[key] = store
        if len(_stores) > 8:
            _, expired = _stores.popitem(last=False)
            expired.client.close()
        return store


def tool_content_store_from_settings(settings):
    """Tool payloads use explicit S3 credentials without migrating local Artifacts."""
    from types import SimpleNamespace
    policy = settings.artifact_storage.model_copy(update={'backend': 's3'})
    values = {name: getattr(settings, name, None) for name in (
        'artifact_s3_endpoint', 'artifact_s3_bucket', 'artifact_s3_access_key', 'artifact_s3_secret_key')}
    return artifact_store_from_settings(SimpleNamespace(artifact_storage=policy, **values))
