#!/usr/bin/env python3
"""Create a private S3-compatible knowledge bucket using Runtime's boto3.

This one-shot has no AgentScope startup side effects. It never prints endpoint,
credentials, bucket policy documents or original SDK exception text.
"""

from dataclasses import dataclass, field
import os
import sys
import time
from urllib.parse import urlsplit


class InitializationError(RuntimeError):
    """An already sanitized diagnostic suitable for process output."""


@dataclass(frozen=True)
class Settings:
    endpoint: str = field(repr=False)
    bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    @classmethod
    def from_environment(cls, environment=None):
        environment = os.environ if environment is None else environment
        names = (
            "KNOWLEDGE_S3_ENDPOINT",
            "KNOWLEDGE_S3_BUCKET",
            "KNOWLEDGE_S3_ACCESS_KEY",
            "KNOWLEDGE_S3_SECRET_KEY",
        )
        values = [environment.get(name, "").strip() for name in names]
        if not all(values):
            raise InitializationError("configuration_missing")
        try:
            endpoint = urlsplit(values[0])
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
            ):
                raise ValueError
            _ = endpoint.port
        except ValueError as exc:
            raise InitializationError("endpoint_invalid") from exc
        return cls(*values)


_SERVICE_CODES = frozenset(
    {
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "ExpiredToken",
        "InvalidToken",
        "NoSuchBucket",
        "NoSuchBucketPolicy",
        "NotFound",
        "404",
        "BucketAlreadyOwnedByYou",
        "BucketAlreadyExists",
        "OperationAborted",
        "ServiceUnavailable",
        "SlowDown",
        "RequestTimeout",
        "InternalError",
        "NotImplemented",
        "InvalidBucketName",
        "InvalidLocationConstraint",
        "IllegalLocationConstraintException",
        "AuthorizationHeaderMalformed",
    }
)
_NETWORK_CODES = frozenset(
    {
        "EndpointConnectionError",
        "ConnectionClosedError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
    }
)
_RETRY_CODES = _NETWORK_CODES | {
    "ServiceUnavailable",
    "SlowDown",
    "RequestTimeout",
    "InternalError",
    "OperationAborted",
}


def _failure(exc):
    """Return code/status from SDK errors without copying untrusted messages."""
    response = getattr(exc, "response", {})
    if isinstance(response, dict):
        error = response.get("Error")
        metadata = response.get("ResponseMetadata")
        code = error.get("Code") if isinstance(error, dict) else None
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        if not isinstance(status, int):
            status = None
        if isinstance(code, str) and code in _SERVICE_CODES:
            return code, status
        if status is not None:
            return f"HTTP{status}", status
    name = type(exc).__name__
    return (name if name in _NETWORK_CODES else "ServiceError"), None


def _ensure_bucket(client, bucket):
    try:
        client.head_bucket(Bucket=bucket)
    except Exception as exc:
        code, status = _failure(exc)
        if status != 404 and code not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        try:
            client.create_bucket(Bucket=bucket)
        except Exception as creation:
            code, _ = _failure(creation)
            if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise
            # Another initializer may have won; existence alone is insufficient.
            client.head_bucket(Bucket=bucket)
    # Deleting a policy is idempotent and removes anonymous grants without ever
    # setting a public ACL. A denied/unsupported API must fail, not claim private.
    try:
        client.delete_bucket_policy(Bucket=bucket)
    except Exception as exc:
        code, _ = _failure(exc)
        if code != "NoSuchBucketPolicy":
            raise


def initialize(
    client,
    bucket,
    *,
    attempts=30,
    delay=2,
    max_elapsed=90,
    sleep=time.sleep,
    clock=time.monotonic,
):
    if attempts < 1 or delay < 0 or max_elapsed <= 0:
        raise InitializationError("retry_configuration_invalid")
    deadline = clock() + max_elapsed
    last_code = "ServiceError"
    for attempt in range(attempts):
        if clock() >= deadline:
            raise InitializationError("bucket_readiness_timeout: " + last_code)
        try:
            _ensure_bucket(client, bucket)
            return
        except Exception as exc:
            code, status = _failure(exc)
            last_code = code
            transient = code in _RETRY_CODES or status in {500, 502, 503, 504}
            if not transient:
                raise InitializationError(
                    "bucket_initialization_failed: " + code
                ) from None
            remaining = deadline - clock()
            if attempt + 1 == attempts or remaining <= 0:
                raise InitializationError("bucket_readiness_timeout: " + code) from None
            sleep(min(delay, remaining))


def make_client(settings):
    # aioboto3 installs aiobotocore[boto3]; Runtime already contains this SDK.
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            connect_timeout=2,
            read_timeout=2,
            retries={"max_attempts": 0},
        ),
    )


def main():
    client = None
    try:
        settings = Settings.from_environment()
        client = make_client(settings)
        initialize(client, settings.bucket)
        print("knowledge bucket initialized; entire bucket policy cleared for the dedicated private bucket")
        return 0
    except InitializationError as exc:
        print("ERROR " + str(exc), file=sys.stderr)
    except Exception as exc:
        code, _ = _failure(exc)
        print("ERROR bucket_initialization_failed: " + code, file=sys.stderr)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return 1


if __name__ == "__main__":
    sys.exit(main())
