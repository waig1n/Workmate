"""Exercise the real CLI and Runner using an inert subprocess boundary."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import deploy
from deploy_config import Finding, Plan
from deploy_probes import guarded


@pytest.fixture
def cli(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/workmate.yaml").write_text("product: {}\n")
    calls = []
    plan = Plan(dependencies=("postgres", "redis"), secrets=("very-private-value",))
    monkeypatch.setattr(deploy, "evaluate", lambda app, compose: plan)

    def run(command, **kwargs):
        args = command[command.index("*") + 1 :]
        calls.append((args, kwargs))
        if args == ["config", "--format", "json"]:
            output = json.dumps(
                {"services": {name: {} for name in (*plan.apps, *plan.dependencies)}}
            )
        elif args == ["ps", "--all", "--format", "json"]:
            output = json.dumps(
                [
                    {"Service": name, "State": "running", "Health": "healthy"}
                    for name in (*plan.apps, *plan.dependencies)
                ]
            )
        else:
            output = "operation OK\n"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    return tmp_path, calls, plan


def invoke(cli, *args):
    return deploy.main(["--root", str(cli[0]), *args])


def test_dry_run_executes_only_read_only_configuration(cli, capsys):
    assert invoke(cli, "up", "--dry-run") == 0
    assert [args for args, _ in cli[1]] == [["config", "--format", "json"]]
    output = capsys.readouterr().out
    assert "DRY-RUN" in output
    assert "--no-deps" in output


def test_invalid_configuration_blocks_all_up_mutations(cli, monkeypatch):
    monkeypatch.setattr(
        deploy,
        "evaluate",
        lambda app, compose: Plan(
            findings=(Finding("error", "bad", "Invalid configuration"),)
        ),
    )
    assert invoke(cli, "up") == 1
    assert [args for args, _ in cli[1]] == [["config", "--format", "json"]]


def test_up_builds_schema_checks_starts_and_diagnoses(cli):
    assert invoke(cli, "--timeout", "30", "up") == 0
    args = [args for args, _ in cli[1]]
    assert args[1] == ["build", *cli[2].apps]
    assert sum(call[0] == "run" for call in args) == 2
    assert args[4][-2:] == ["postgres", "redis"]
    assert sum(call[0] == "up" for call in args) == 4
    assert sum(call[0] == "exec" for call in args) == 8
    assert all("--no-deps" in call for call in args if call[0] in ("up", "run"))
    product_ready = next(i for i, call in enumerate(args) if call[:4] == ['exec', '-T', 'gateway', 'python'])
    worker_start = next(i for i, call in enumerate(args) if call[0] == 'up' and call[-1] == 'runtime')
    assert product_ready < worker_start
    schemas = [kwargs['input'] for call, kwargs in cli[1] if call[0] == 'run']
    assert any('load_worker_role_settings' in script for script in schemas)
    assert all('agentscope-runtime' not in call for call in args)
    assert all('/workmate/security/capabilities' not in (kwargs.get('input') or '') for _, kwargs in cli[1])


def test_worktree_override_is_explicitly_loaded_for_isolated_ports_and_volumes(cli, monkeypatch):
    override = cli[0] / 'docker-compose.override.yml'
    override.write_text('services: {}\n')
    commands = []
    original = deploy.subprocess.run
    def capture(command, **kwargs):
        commands.append(command)
        return original(command, **kwargs)
    monkeypatch.setattr(deploy.subprocess, 'run', capture)
    assert invoke(cli, 'up', '--dry-run') == 0
    assert commands[0][commands[0].index(str(override)) - 1] == '-f'
    assert str(cli[0] / 'docker-compose.yml') in commands[0]


@pytest.mark.parametrize('service,role', [('gateway', 'product-api'), ('runtime', 'worker')])
def test_renamed_services_preserve_health_protocol(monkeypatch, service, role):
    import urllib.request
    payload = json.dumps({'role': role, 'ready': True}).encode()
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: SimpleNamespace(read=lambda: payload))
    exec(deploy.health_probe(service, 'http://localhost/health'), {'urllib': __import__('urllib')})


@pytest.mark.parametrize('payload', [{'role': 'legacy', 'ready': True}, {'role': 'worker', 'ready': False}])
def test_worker_probe_rejects_wrong_role_or_unready_health(monkeypatch, payload):
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: SimpleNamespace(read=lambda: json.dumps(payload).encode()))
    with pytest.raises(RuntimeError, match='not ready'):
        exec(deploy.health_probe('runtime', 'http://worker/health'), {'urllib': __import__('urllib')})


@pytest.mark.parametrize('authenticated_status', [404, 403])
def test_native_auth_probe_verifies_token_without_touching_knowledge_data(monkeypatch, authenticated_status):
    import urllib.request
    from urllib.error import HTTPError
    monkeypatch.setenv('RUNTIME_BASE_URL', 'http://runtime:8000')
    monkeypatch.setenv('RUNTIME_INTERNAL_TOKEN', 'private-service-token')
    seen = []
    def open_request(request, **kwargs):
        if isinstance(request, str):
            assert request.endswith('/health')
            return SimpleNamespace(read=lambda: b'{"role":"worker","ready":true}')
        seen.append(request)
        assert request.full_url.endswith('/__workmate_deployment_auth_probe__')
        code = authenticated_status if request.get_header('X-workmate-internal-token') else 403
        raise HTTPError(request.full_url, code, '', {}, None)
    monkeypatch.setattr(urllib.request, 'urlopen', open_request)
    scope = {'os': __import__('os'), 'urllib': __import__('urllib')}
    if authenticated_status == 404:
        exec(deploy.WORKER_AUTH, scope)
    else:
        with pytest.raises(RuntimeError, match='authentication failed'):
            exec(deploy.WORKER_AUTH, scope)
    assert len(seen) == 2


def test_no_build_and_check_variants(cli):
    assert invoke(cli, "up", "--no-build") == 0
    assert not any(args[0] == "build" for args, _ in cli[1])
    cli[1].clear()
    assert invoke(cli, "check") == 0
    assert len(cli[1]) == 1
    cli[1].clear()
    assert invoke(cli, "check", "--deep") == 0
    assert sum(args[0] == "run" for args, _ in cli[1]) == 2


def test_stop_preserves_volumes_and_only_runs_stop(cli):
    assert invoke(cli, "stop") == 0
    args = [args for args, _ in cli[1]]
    assert args == [["config", "--format", "json"], ["stop"]]
    assert not any(
        item in {"down", "rm", "--volumes", "-v"} for call in args for item in call
    )


def test_status_logs_and_unknown_service(cli):
    assert invoke(cli, "status") == 0
    assert cli[1][-1][0] == ["ps", "--all"]
    assert invoke(cli, "logs", "--tail", "12", "gateway") == 0
    assert cli[1][-1][0] == ["logs", "--no-color", "--tail", "12", "gateway"]
    assert invoke(cli, "logs", "does-not-exist") == 1


@pytest.mark.parametrize('state,code', [
    ('candidate_requires_external_verification', 0), ('blocked', 2), ('unknown', 2),
])
def test_upgrade_check_only_reads_running_runtime_and_preserves_status(cli, monkeypatch, state, code):
    original = deploy.subprocess.run
    def inspect(command, **kwargs):
        if 'workmate_runtime.operations.upgrade_check' in command:
            args = command[command.index('*') + 1:]
            cli[1].append((args, kwargs))
            return SimpleNamespace(returncode=code, stderr='', stdout=json.dumps({
                'drain_state':state, 'upgrade_authorized':False}))
        return original(command, **kwargs)
    monkeypatch.setattr(deploy.subprocess, 'run', inspect)
    assert invoke(cli, 'upgrade-check', '--storage-node-id', 'node-a', '--limit', '25') == code
    assert [args for args, _ in cli[1]] == [
        ['config', '--format', 'json'],
        ['exec', '-T', 'runtime', 'python', '-m', 'workmate_runtime.operations.upgrade_check',
            '--storage-node-id', 'node-a', '--limit', '25'],
    ]


@pytest.mark.parametrize('args', [
    ('upgrade-check', '--storage-node-id', 'node', '--limit', '0'),
    ('upgrade-check', '--storage-node-id', 'node', '--limit', '1001'),
    ('upgrade-check', '--storage-node-id', ''),
])
def test_upgrade_check_invalid_bounds_never_contact_docker(cli, args):
    with pytest.raises(SystemExit) as error:
        invoke(cli, *args)
    assert error.value.code == 2
    assert not cli[1]


@pytest.mark.parametrize('report', [[], {'upgrade_authorized':True,
    'drain_state':'candidate_requires_external_verification'}, {'drain_state':'unknown'}])
def test_upgrade_check_rejects_invalid_or_authorizing_report(cli, monkeypatch, report):
    original = deploy.subprocess.run
    def inspect(command, **kwargs):
        if 'workmate_runtime.operations.upgrade_check' in command:
            return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(report))
        return original(command, **kwargs)
    monkeypatch.setattr(deploy.subprocess, 'run', inspect)
    assert invoke(cli, 'upgrade-check', '--storage-node-id', 'node-a') == 1


@pytest.mark.parametrize(
    "args",
    [
        ("--timeout", "0", "status"),
        ("logs", "--tail", "0"),
        ("logs", "--tail", "10001"),
    ],
)
def test_invalid_argument_bounds_reject_before_docker(cli, args):
    with pytest.raises(SystemExit) as error:
        invoke(cli, *args)
    assert error.value.code == 2
    assert not cli[1]


def test_doctor_aggregates_container_and_probe_failures(capsys):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "ps":
            return json.dumps(
                [
                    {
                        "Service": "gateway",
                        "State": "running",
                        "Health": "starting",
                    },
                    {"Service": "unused", "State": "running"},
                ]
            )
        raise RuntimeError("probe unavailable")

    assert deploy.doctor(run, Plan()) is False
    assert sum(args[0] == "exec" for args in calls) == 5
    output = capsys.readouterr().out
    assert "WARNING" in output and "unused" in output
    assert "ERROR" in output and "frontend" in output


def test_doctor_command_returns_failed_state(cli, monkeypatch):
    monkeypatch.setattr(deploy, "doctor", lambda run, plan, expected_heads=(): False)
    assert invoke(cli, "doctor") == 1
    assert invoke(cli, "up", "--no-build") == 1


def test_runner_timeout_and_redacted_failure(monkeypatch, capsys):
    runner = deploy.Runner(["docker", "compose"], timeout=9)
    runner.redact = deploy.Redactor(("very-private-value",))

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 9
        raise subprocess.TimeoutExpired(args[0], 9, output="very-private-value")

    monkeypatch.setattr(deploy.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out") as error:
        runner(["ps"])
    assert "very-private-value" not in str(error.value)
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=2, stdout="very-private-value", stderr="password=hidden"
        ),
    )
    with pytest.raises(RuntimeError) as error:
        runner(["logs"])
    assert "very-private-value" not in str(error.value)
    assert "hidden" not in str(error.value)
    with pytest.raises(RuntimeError) as error:
        runner(["config"], quiet=True)
    assert "[REDACTED]" not in str(error.value)  # config output omitted entirely
    assert "very-private-value" not in capsys.readouterr().out


def test_runner_stdout_redaction_and_return_value(monkeypatch, capsys):
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="secret-value\n", stderr=""
        ),
    )
    runner = deploy.Runner(["docker"])
    runner.redact = deploy.Redactor(("secret-value",))
    assert runner(["logs"]) == "secret-value\n"
    assert "secret-value" not in capsys.readouterr().out


def test_cli_reports_yaml_error_without_contents(cli, capsys):
    (cli[0] / "config/workmate.yaml").write_text("secret-value: [bad syntax")
    assert invoke(cli, "check") == 1
    output = capsys.readouterr().err
    assert "YAML" in output
    assert "secret-value" not in output


def test_cli_redacts_operation_errors(cli, monkeypatch, capsys):
    original = deploy.subprocess.run

    def fail(command, **kwargs):
        if command[-1] == "stop":
            return SimpleNamespace(returncode=1, stdout="", stderr="very-private-value")
        return original(command, **kwargs)

    monkeypatch.setattr(deploy.subprocess, "run", fail)
    assert invoke(cli, "stop") == 1
    output = capsys.readouterr().err
    assert "very-private-value" not in output
    assert "[REDACTED]" in output


def test_empty_states_and_guarded_probe_exception_is_sanitized(capsys):
    assert deploy.parse_states("  ") == []
    with pytest.raises(SystemExit) as error:
        exec(guarded("raise ValueError('secret-value')"), {})
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert "ValueError" in output
    assert "secret-value" not in output


@pytest.mark.parametrize("command", ["logs", "status", "stop"])
def test_recovery_commands_work_with_broken_yaml(cli, command):
    (cli[0] / "config/workmate.yaml").write_text("broken: [yaml")
    assert invoke(cli, command) == 0


def test_readiness_retries_then_continues_without_unbounded_sleep(monkeypatch):
    class Run:
        dry_run = False

        def __init__(self):
            self.calls = []

        def __call__(self, args, **kwargs):
            self.calls.append((args, kwargs))
            if len(self.calls) == 1:
                raise RuntimeError("not yet")

    run = Run()
    ticks = iter((0, 0, 1, 2))
    monkeypatch.setattr(deploy.time, "monotonic", lambda: next(ticks))
    sleeps = []
    monkeypatch.setattr(deploy.time, "sleep", sleeps.append)
    deploy.readiness(run, "gateway", 10)
    assert len(run.calls) == 2
    assert sleeps == [1]
    assert all(1 <= kwargs["timeout"] <= 5 for _, kwargs in run.calls)


def test_readiness_deadline_stops_following_app_start(monkeypatch):
    class Run:
        dry_run = False

        def __init__(self):
            self.calls = []

        def __call__(self, args, **kwargs):
            self.calls.append(args)
            if args[0] == "exec":
                raise RuntimeError("not ready")

    ticks = iter((0, 0, 10))
    monkeypatch.setattr(deploy.time, "monotonic", lambda: next(ticks))
    run = Run()
    with pytest.raises(RuntimeError, match="timed out"):
        deploy.launch(
            run,
            Plan(apps=("gateway", "runtime")),
            5,
            ready=deploy.readiness,
        )
    assert not any(args[-1] == "runtime" for args in run.calls)


def test_source_heads_reads_migration_graph_without_executing_files(tmp_path):
    versions = tmp_path / "backend/alembic/versions"
    versions.mkdir(parents=True)
    (versions / "first.py").write_text(
        "revision = '0001'\ndown_revision = None\nraise RuntimeError('must never execute')\n"
    )
    (versions / "branch_a.py").write_text(
        "revision = '0002a'\ndown_revision = '0001'\n"
    )
    (versions / "branch_b.py").write_text(
        "revision = '0002b'\ndown_revision = '0001'\n"
    )
    assert deploy.source_heads(tmp_path) == ["0002a", "0002b"]
    (versions / "merge.py").write_text(
        "revision: str = '0003'\ndown_revision: tuple[str, ...] = ('0002a','0002b')\n"
    )
    assert deploy.source_heads(tmp_path) == ["0003"]
    assert deploy.source_heads(tmp_path / "missing") == []


def test_probe_code_and_schema_scripts_compile():
    from deploy_probes import DATABASE, DEPENDENCIES

    compile(guarded("EXPECTED_HEADS = []\n" + DATABASE), "<database-probe>", "exec")
    compile(guarded(DEPENDENCIES), "<dependencies-probe>", "exec")
    calls = []
    deploy.schema_check(lambda args, **kwargs: calls.append(kwargs["input"]))
    assert len(calls) == 2
    for script in calls:
        compile(script, "<schema-probe>", "exec")


def test_runtime_health_probes_send_required_user_header(monkeypatch):
    import urllib.request

    requests = []
    monkeypatch.setattr(urllib.request, "install_opener", lambda opener: None)

    def urlopen(request, **kwargs):
        requests.append(request)
        return SimpleNamespace(read=lambda: b'{"role":"worker","ready":true}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    class Run:
        dry_run = False

        def __init__(self):
            self.scripts = []

        def __call__(self, args, **kwargs):
            if args[0] == "ps":
                return json.dumps(
                    [{"Service": name, "State": "running"} for name in Plan().apps]
                )
            if "-c" in args:
                exec(args[-1], {})
            elif kwargs.get("input"):
                self.scripts.append(kwargs["input"])

    run = Run()
    deploy.readiness(run, "runtime", 10)
    assert deploy.doctor(run, Plan(), expected_heads=("0016",))
    health = next(script for script in run.scripts if "127.0.0.1:8000/health" in script)
    exec(health, {})
    assert len(requests) == 2
    assert all(
        request.get_header("X-user-id") == "deployment-doctor" for request in requests
    )
    database = next(script for script in run.scripts if "EXPECTED_HEADS" in script)
    assert "['0016']" in database
    for script in run.scripts:
        compile(script, "<doctor-probe>", "exec")


def security_warning_plan(monkeypatch, *, blocking_error=False):
    findings = [
        Finding("warning", "weak_credential", "Credential is weak.", security=True)
    ]
    if blocking_error:
        findings.append(
            Finding("error", "internal_token_mismatch", "Service tokens differ.")
        )
    plan = Plan(findings=tuple(findings))
    monkeypatch.setattr(deploy, "evaluate", lambda app, compose: plan)
    return plan


@pytest.mark.parametrize("command", ["check", "doctor"])
def test_security_warnings_do_not_block_read_only_diagnostics(
    cli, monkeypatch, command
):
    security_warning_plan(monkeypatch)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail("diagnostics must not request permission"),
    )
    assert invoke(cli, command) == 0
    assert not any(args[0] in {"build", "up", "run", "stop"} for args, _ in cli[1])


@pytest.mark.parametrize("answer", ["", "n", "no"])
def test_security_confirmation_defaults_to_no_before_mutations(
    cli, monkeypatch, answer
):
    security_warning_plan(monkeypatch)
    monkeypatch.setattr(deploy.sys.stdin, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr(
        "builtins.input", lambda prompt: prompts.append(prompt) or answer
    )
    assert invoke(cli, "up", "--no-build") == 1
    assert len(prompts) == 1
    assert [args for args, _ in cli[1]] == [["config", "--format", "json"]]


@pytest.mark.parametrize("answer", ["y", "yes", "YES"])
def test_explicit_interactive_security_confirmation_allows_up(cli, monkeypatch, answer):
    security_warning_plan(monkeypatch)
    monkeypatch.setattr(deploy.sys.stdin, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr(
        "builtins.input", lambda prompt: prompts.append(prompt) or answer
    )
    assert invoke(cli, "up", "--no-build") == 0
    assert len(prompts) == 1
    assert any(args[0] == "up" for args, _ in cli[1])


def test_noninteractive_security_warning_requires_explicit_flag(
    cli, monkeypatch, capsys
):
    security_warning_plan(monkeypatch)
    monkeypatch.setattr(deploy.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail("noninteractive deployment must not read input"),
    )
    assert invoke(cli, "up") == 1
    assert [args for args, _ in cli[1]] == [["config", "--format", "json"]]
    captured = capsys.readouterr()
    assert "--skip-security-checks" in captured.out + captured.err


def test_explicit_skip_allows_weak_credentials_without_prompt(cli, monkeypatch):
    security_warning_plan(monkeypatch)
    monkeypatch.setattr(deploy.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input", lambda prompt: pytest.fail("explicit skip must not prompt")
    )
    assert invoke(cli, "up", "--skip-security-checks", "--no-build") == 0
    assert any(args[0] == "up" for args, _ in cli[1])


def test_skip_security_never_bypasses_configuration_errors(cli, monkeypatch):
    security_warning_plan(monkeypatch, blocking_error=True)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail(
            "invalid configuration must fail before confirmation"
        ),
    )
    assert invoke(cli, "up", "--skip-security-checks") == 1
    assert [args for args, _ in cli[1]] == [["config", "--format", "json"]]


def test_security_dry_run_requires_no_confirmation_and_performs_no_mutation(
    cli, monkeypatch
):
    security_warning_plan(monkeypatch)
    monkeypatch.setattr(deploy.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail("dry run must not request approval"),
    )
    assert invoke(cli, "up", "--dry-run") == 0
    assert [args for args, _ in cli[1]] == [["config", "--format", "json"]]
