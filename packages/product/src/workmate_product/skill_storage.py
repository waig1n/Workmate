"""Private, content-addressed Skill bytes; no Product authorization is implied.

Application composition injects this dependency into session.info. The store
never creates buckets, publishes ACLs, deletes objects or overwrites a key.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
from typing import AsyncIterator, Protocol

from .application.skill_artifacts import MAX_ARCHIVE_BYTES, SkillArtifactError


def key_for_digest(digest: str) -> str:
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise SkillArtifactError('Invalid Skill artifact digest')
    return f'sha256/{digest[:2]}/{digest}.zip'


@dataclass(frozen=True)
class BlobObject:
    key: str
    size_bytes: int
    last_modified: datetime


class SkillBlobStore(Protocol):
    async def put_verified(self, digest: str, raw: bytes) -> str: ...
    async def get_verified(self, digest: str, size_bytes: int) -> bytes: ...
    def list_objects(self) -> AsyncIterator[BlobObject]: ...


def get_skill_blob_store(db) -> SkillBlobStore:
    store = db.info.get('skill_blob_store')
    if store is None:
        raise SkillArtifactError('Skill object storage is not configured')
    return store


def store_from_settings(settings) -> SkillBlobStore:
    return S3SkillBlobStore(
        endpoint=settings.skill_s3_endpoint, bucket=settings.skill_s3_bucket,
        access_key=settings.skill_s3_access_key, secret_key=settings.skill_s3_secret_key,
        region=settings.skill_storage.region,
        timeout_seconds=settings.skill_storage.timeout_seconds,
    )


class PreparedSkillBlobStore:
    """Request-scoped verified publications, prepared before business locks.

    It only reuses bytes whose conditional PUT and GET verification have already
    completed. Never share this wrapper across requests or retain it as a cache.
    """
    def __init__(self, delegate):
        self.delegate = delegate
        self._verified = {}

    async def prepare(self, raw):
        from .application.skill_artifacts import parse_skill_archive
        artifact = parse_skill_archive(raw)
        key = await self.delegate.put_verified(artifact.digest, raw)
        if key != key_for_digest(artifact.digest):
            raise SkillArtifactError('Skill object storage returned an invalid key')
        self._verified[artifact.digest] = raw
        return artifact

    async def put_verified(self, digest, raw):
        if self._verified.get(digest) == raw:
            return key_for_digest(digest)
        return await self.delegate.put_verified(digest, raw)

    async def get_verified(self, digest, size_bytes):
        raw = self._verified.get(digest)
        if raw is not None:
            S3SkillBlobStore._verify(raw, digest, size_bytes)
            return raw
        return await self.delegate.get_verified(digest, size_bytes)

    def list_objects(self):
        return self.delegate.list_objects()


class S3SkillBlobStore:
    def __init__(self, *, endpoint, bucket, access_key, secret_key,
                 region='us-east-1', timeout_seconds=30, client=None):
        self._endpoint, self._bucket = endpoint, bucket
        self._access_key, self._secret_key = access_key, secret_key
        self._region, self._timeout = region, timeout_seconds
        self._injected_client = client

    def _client(self):
        if self._injected_client is not None:
            return self._injected_client
        if not all((self._endpoint, self._bucket, self._access_key, self._secret_key)):
            raise SkillArtifactError('Skill object storage credentials and endpoint are required')
        import boto3
        from botocore.config import Config
        # Explicit credentials avoid instance metadata/default credential discovery.
        return boto3.session.Session().client(
            's3', endpoint_url=self._endpoint, region_name=self._region,
            aws_access_key_id=self._access_key, aws_secret_access_key=self._secret_key,
            config=Config(signature_version='s3v4', connect_timeout=self._timeout,
                          read_timeout=self._timeout, retries={'total_max_attempts': 2},
                          s3={'addressing_style': 'path'}),
        )

    @staticmethod
    def _verify(raw, digest, size):
        if (not isinstance(raw, bytes) or type(size) is not int
                or not 0 < size <= MAX_ARCHIVE_BYTES or len(raw) != size
                or hashlib.sha256(raw).hexdigest() != digest):
            raise SkillArtifactError('Skill object integrity check failed')

    def _read(self, client, digest, size):
        key = key_for_digest(digest)
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise SkillArtifactError('Invalid Skill object size')
        response = client.get_object(Bucket=self._bucket, Key=key)
        body = response['Body']
        try:
            if response['ContentLength'] != size:
                raise SkillArtifactError('Skill object size mismatch')
            raw = body.read(size + 1)
        finally:
            body.close()
        self._verify(raw, digest, size)
        return raw

    def _operation(self, action):
        client = None
        try:
            client = self._client()
            return action(client)
        except SkillArtifactError:
            raise
        except Exception:
            # SDK exceptions may contain endpoint details; report a safe category.
            raise SkillArtifactError('Skill object storage is unavailable') from None
        finally:
            if client is not None and self._injected_client is None:
                client.close()

    async def get_verified(self, digest, size_bytes):
        return await asyncio.to_thread(self._operation, lambda c: self._read(c, digest, size_bytes))

    async def put_verified(self, digest, raw):
        key = key_for_digest(digest)
        self._verify(raw, digest, len(raw))

        def write(client):
            from botocore.exceptions import ClientError
            try:
                client.put_object(Bucket=self._bucket, Key=key, Body=raw,
                                  ContentType='application/zip', IfNoneMatch='*')
            except ClientError as exc:
                code = exc.response.get('Error', {}).get('Code')
                status = exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode')
                if code not in {'PreconditionFailed', 'ConditionalRequestConflict'} and status not in {409, 412}:
                    raise
            # Also verifies an existing object after a concurrent publisher wins.
            # Never infer object integrity from ETag or caller-provided metadata.
            self._read(client, digest, len(raw))
            return key

        return await asyncio.to_thread(self._operation, write)

    async def list_objects(self):
        token = None
        while True:
            args = {'Bucket': self._bucket, 'Prefix': 'sha256/', 'MaxKeys': 1000}
            if token:
                args['ContinuationToken'] = token
            page = await asyncio.to_thread(self._operation, lambda c: c.list_objects_v2(**args))
            for item in page.get('Contents', []):
                yield BlobObject(item['Key'], item['Size'], item['LastModified'])
            if not page.get('IsTruncated'):
                return
            next_token = page.get('NextContinuationToken')
            if not next_token or next_token == token:
                raise SkillArtifactError('Invalid Skill object listing continuation')
            token = next_token
