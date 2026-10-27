import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1]))
from deploy import Redactor, launch, parse_states


def test_redacts_credentials():
    redact = Redactor(["topsecret"])
    result = redact(
        "topsecret https://user:password@host Authorization: Bearer abc123 API_KEY=abcdef"
    )
    assert all(
        value not in result for value in ("topsecret", "password", "abc123", "abcdef")
    )


def test_states_support_compose_json_and_json_lines():
    assert parse_states('[{"Service":"a"}]') == [{"Service": "a"}]
    assert len(parse_states('{"Service":"a"}\n{"Service":"b"}')) == 2


def test_launch_preserves_gate_and_orders_init():
    calls = []
    runner = lambda args, **kwargs: calls.append(args)
    plan = SimpleNamespace(
        dependencies=("postgres", "rustfs"),
        init_services=("storage-init",),
        apps=("gateway", "runtime", "frontend"),
    )
    launch(runner, plan, 120)
    assert calls[0] == [
        "up",
        "-d",
        "--no-deps",
        "--wait",
        "--wait-timeout",
        "120",
        "postgres",
        "rustfs",
    ]
    assert calls[1] == ["run", "--rm", "--no-deps", "-T", "storage-init"]
    assert calls[2][-1] == "gateway"
    assert calls[3][-1] == "runtime"
    assert not any("uvicorn" in call or "upgrade" in call for call in calls)


def test_init_failure_prevents_apps():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if args[0] == "run":
            raise RuntimeError("init failed")

    import pytest

    with pytest.raises(RuntimeError):
        launch(
            runner,
            SimpleNamespace(
                dependencies=(), init_services=("storage-init",), apps=("gateway",)
            ),
            120,
        )
    assert len(calls) == 1
