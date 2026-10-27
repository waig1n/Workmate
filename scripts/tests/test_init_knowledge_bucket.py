"""Bucket initialization never confuses authorization failure with absence."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from init_knowledge_bucket import Settings, InitializationError, initialize, main


class ClientError(Exception):
    def __init__(self, response):
        super().__init__("private-key https://private-endpoint")
        self.response = response


class EndpointConnectionError(Exception):
    pass


def error(code, status=400):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "private-key https://private-endpoint"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }
    )


def test_requires_all_credentials_and_rejects_embedded_url_secrets():
    env = {
        "KNOWLEDGE_S3_ENDPOINT": "http://rustfs:9000",
        "KNOWLEDGE_S3_BUCKET": "knowledge",
        "KNOWLEDGE_S3_ACCESS_KEY": "access",
        "KNOWLEDGE_S3_SECRET_KEY": "private-key",
    }
    assert Settings.from_environment(env).bucket == "knowledge"
    assert "private-key" not in repr(Settings.from_environment(env))
    for key in env:
        with pytest.raises(InitializationError):
            Settings.from_environment(env | {key: " "})
    with pytest.raises(InitializationError):
        Settings.from_environment(
            env | {"KNOWLEDGE_S3_ENDPOINT": "http://user:private-key@rustfs:9000"}
        )


def test_existing_bucket_is_idempotent_and_public_policy_removed():
    client = Mock()
    initialize(client, "knowledge", sleep=lambda _: None)
    client.head_bucket.assert_called_once_with(Bucket="knowledge")
    client.create_bucket.assert_not_called()
    client.delete_bucket_policy.assert_called_once_with(Bucket="knowledge")
    client.put_bucket_acl.assert_not_called()


def test_missing_bucket_created_then_policy_removed():
    client = Mock()
    client.head_bucket.side_effect = error("404", 404)
    initialize(client, "knowledge", sleep=lambda _: None)
    client.create_bucket.assert_called_once_with(Bucket="knowledge")
    client.delete_bucket_policy.assert_called_once_with(Bucket="knowledge")


@pytest.mark.parametrize(
    "code,status",
    [
        ("AccessDenied", 403),
        ("InvalidAccessKeyId", 403),
        ("SignatureDoesNotMatch", 403),
    ],
)
def test_authentication_failure_does_not_create_bucket(code, status):
    client = Mock()
    client.head_bucket.side_effect = error(code, status)
    with pytest.raises(InitializationError) as failure:
        initialize(client, "knowledge", sleep=lambda _: None)
    assert code in str(failure.value)
    assert "private-key" not in str(failure.value)
    assert client.head_bucket.call_count == 1
    client.create_bucket.assert_not_called()


def test_create_race_rechecks_access_instead_of_claiming_success():
    client = Mock()
    client.head_bucket.side_effect = [error("404", 404), {}]
    client.create_bucket.side_effect = error("BucketAlreadyOwnedByYou", 409)
    initialize(client, "knowledge", sleep=lambda _: None)
    assert client.head_bucket.call_count == 2
    client.delete_bucket_policy.assert_called_once()


def test_bucket_race_with_other_owner_fails_closed():
    client = Mock()
    client.head_bucket.side_effect = [error("404", 404), error("AccessDenied", 403)]
    client.create_bucket.side_effect = error("BucketAlreadyExists", 409)
    with pytest.raises(InitializationError):
        initialize(client, "knowledge", sleep=lambda _: None)
    client.delete_bucket_policy.assert_not_called()


def test_network_readiness_retries_are_bounded():
    client = Mock()
    client.head_bucket.side_effect = EndpointConnectionError(
        "https://private-endpoint/private-key"
    )
    sleeps = []
    with pytest.raises(InitializationError) as failure:
        initialize(client, "knowledge", attempts=3, delay=2, sleep=sleeps.append)
    assert client.head_bucket.call_count == 3
    assert sleeps == [2, 2]
    assert "private-" not in str(failure.value)
    client.create_bucket.assert_not_called()


def test_ready_after_transient_failure_and_absent_policy_are_success():
    client = Mock()
    client.head_bucket.side_effect = [error("ServiceUnavailable", 503), {}]
    client.delete_bucket_policy.side_effect = error("NoSuchBucketPolicy", 404)
    initialize(client, "knowledge", sleep=lambda _: None)
    assert client.head_bucket.call_count == 2


def test_policy_api_unavailable_never_reports_private():
    client = Mock()
    client.delete_bucket_policy.side_effect = error("NotImplemented", 501)
    with pytest.raises(InitializationError, match="NotImplemented"):
        initialize(client, "knowledge", sleep=lambda _: None)


def test_main_returns_nonzero_and_never_echoes_exception_details(monkeypatch, capsys):
    monkeypatch.setattr(
        Settings,
        "from_environment",
        Mock(side_effect=InitializationError("configuration_missing")),
    )
    assert main() == 1
    assert "configuration_missing" in capsys.readouterr().err


def test_deadline_stops_before_starting_another_attempt():
    client = Mock()
    client.head_bucket.side_effect = error("ServiceUnavailable", 503)
    timestamps = iter([0, 0, 1, 3])
    sleeps = []
    with pytest.raises(InitializationError, match="bucket_readiness_timeout"):
        initialize(
            client,
            "knowledge",
            max_elapsed=3,
            delay=2,
            clock=lambda: next(timestamps),
            sleep=sleeps.append,
        )
    assert client.head_bucket.call_count == 1
    assert sleeps == [2]


@pytest.mark.parametrize(
    "response",
    [
        {"Error": None},
        {"Error": {"Code": []}},
        {"Error": {}, "ResponseMetadata": None},
    ],
)
def test_malformed_error_is_sanitized(response):
    client = Mock()
    client.head_bucket.side_effect = ClientError(response)
    with pytest.raises(InitializationError, match="ServiceError"):
        initialize(client, "knowledge")
    client.create_bucket.assert_not_called()


def test_main_success_closes_client(monkeypatch, capsys):
    import init_knowledge_bucket as module

    client = Mock()
    monkeypatch.setattr(
        Settings,
        "from_environment",
        Mock(
            return_value=Settings(
                "http://rustfs:9000", "knowledge", "access", "private-key"
            )
        ),
    )
    monkeypatch.setattr(module, "make_client", Mock(return_value=client))
    assert main() == 0
    client.close.assert_called_once()
    assert "initialized" in capsys.readouterr().out


def test_main_unexpected_exception_redacted_and_client_closed(monkeypatch, capsys):
    import init_knowledge_bucket as module

    client = Mock()
    monkeypatch.setattr(
        Settings,
        "from_environment",
        Mock(
            return_value=Settings(
                "http://rustfs:9000", "knowledge", "access", "private-key"
            )
        ),
    )
    monkeypatch.setattr(module, "make_client", Mock(return_value=client))
    monkeypatch.setattr(
        module, "initialize", Mock(side_effect=RuntimeError("private-key"))
    )
    assert main() == 1
    client.close.assert_called_once()
    output = capsys.readouterr().err
    assert "ServiceError" in output
    assert "private-key" not in output
