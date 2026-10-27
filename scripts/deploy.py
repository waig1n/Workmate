#!/usr/bin/env python3
"""Bounded Docker Compose deployment operations; never deletes volumes."""

import ast
import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from deploy_config import evaluate
from deploy_probes import DATABASE, DEPENDENCIES, guarded


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = sorted(set(filter(None, secrets)), key=len, reverse=True)

    def __call__(self, value):
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(://[^\s/:]+:)[^\s@]+@", r"\1[REDACTED]@", value)
        value = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", value)
        return re.sub(
            r'(?i)((?:[\w-]*(?:password|secret|token|api_key|access_key)[\w-]*)[\s"\x27]*[:=]\s*["\x27]?)[^\s,"\x27&]+',
            r"\1[REDACTED]",
            value,
        )


def parse_states(output):
    if not output.strip():
        return []
    if output.lstrip().startswith("["):
        return json.loads(output)
    return [json.loads(line) for line in output.splitlines() if line.strip()]


class Runner:
    def __init__(self, base, timeout=120, dry_run=False):
        self.base, self.timeout, self.dry_run = base, timeout, dry_run
        self.redact = Redactor()

    def __call__(self, args, *, input=None, mutation=False, quiet=False, timeout=None,
                 accepted_returncodes=(0,)):
        command = self.base + list(args)
        if mutation and self.dry_run:
            print("DRY-RUN " + shlex.join(command))
            return ""
        if not quiet:
            print("$ " + self.redact(shlex.join(command)), flush=True)
        try:
            result = subprocess.run(
                command,
                input=input,
                text=True,
                capture_output=True,
                timeout=timeout or self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "Command timed out; inspect status / logs. Services already started will not be rolled back automatically."
            ) from exc
        if result.returncode not in accepted_returncodes:
            # Raw config can contain arbitrary credentials: never echo its output.
            detail = (
                "Compose configuration parsing failed; check the Docker Compose version, environment file path, and YAML syntax."
                if args and args[0] == "config"
                else self.redact(result.stderr + result.stdout)[-6000:]
            )
            raise RuntimeError(f"Command failed (exit {result.returncode}) {detail}")
        if not quiet and result.stdout:
            print(self.redact(result.stdout).rstrip())
        return result.stdout


def launch(run, plan, timeout, ready=None):
    if plan.dependencies:
        run(
            [
                "up",
                "-d",
                "--no-deps",
                "--wait",
                "--wait-timeout",
                str(timeout),
                *plan.dependencies,
            ],
            mutation=True,
            timeout=timeout + 30,
        )
    for service in plan.init_services:
        run(["run", "--rm", "--no-deps", "-T", service], mutation=True)
    for service in plan.apps:
        run(
            [
                "up",
                "-d",
                "--no-deps",
                "--wait",
                "--wait-timeout",
                str(timeout),
                service,
            ],
            mutation=True,
            timeout=timeout + 30,
        )
        if ready:
            ready(run, service, timeout)


def health_probe(service, url, timeout=10):
    # Deployment names do not change the application's health protocol.
    role = {"runtime": "worker", "gateway": "product-api"}[service]
    return (
        "import json\n"
        f"payload=json.loads(urllib.request.urlopen(urllib.request.Request({url!r}, headers={{'X-User-ID':'deployment-doctor'}}), timeout={timeout}).read())\n"
        f"if payload.get('role') != {role!r} or payload.get('ready') is not True: raise RuntimeError('Native role is not ready')\n"
    )


def readiness(run, service, timeout):
    if run.dry_run:
        print("DRY-RUN wait for HTTP readiness: " + service)
        return
    port = {"runtime": 8000, "gateway": 8080, "frontend": 3000}[service]
    path = {
        "runtime": "/health",
        "gateway": "/health",
        "frontend": "/",
    }[service]
    url = f"http://127.0.0.1:{port}{path}"
    if service == "frontend":
        command = [
            "exec",
            "-T",
            service,
            "node",
            "-e",
            f"fetch('{url}',{{signal:AbortSignal.timeout(2000)}}).then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
        ]
    else:
        command = [
            "exec",
            "-T",
            service,
            "python",
            "-c",
            "import urllib.request; urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))\n" + health_probe(service, url, 2),
        ]
    deadline = time.monotonic() + timeout
    while True:
        try:
            run(
                command, quiet=True, timeout=max(1, min(5, deadline - time.monotonic()))
            )
            print(service + ": HTTP ready")
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    service
                    + ": HTTP readiness timed out; check logs and ensure database migrations have completed"
                )
            time.sleep(1)


SCHEMA = """
import sys
try:
    {loader}
except Exception as exc:
    print("schema validation failed: " + type(exc).__name__)
    if hasattr(exc, 'errors'):
        for error in exc.errors():
            print('.'.join(map(str,error['loc'])) + ': ' + error['type'])
    sys.exit(1)
print('schema OK')
"""


def schema_check(run):
    for service, loader in (
        (
            "gateway",
            "from app.config import get_settings; get_settings()",
        ),
        (
            "runtime",
            "from workmate_runtime.roles.settings import load_worker_role_settings; load_worker_role_settings()",
        ),
    ):
        run(
            ["run", "--rm", "--no-deps", "-T", "--entrypoint", "python", service, "-"],
            input=SCHEMA.format(loader=loader),
            mutation=True,
        )


def source_heads(root):
    revisions, parents = set(), set()
    for path in (root / "backend/alembic/versions").glob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.AnnAssign):
                target = node.target
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            else:
                continue
            if isinstance(target, ast.Name) and target.id in (
                "revision",
                "down_revision",
            ):
                value = ast.literal_eval(node.value)
                if value:
                    (revisions if target.id == "revision" else parents).update(
                        (value,) if isinstance(value, str) else value
                    )
    return sorted(revisions - parents)


WORKER_AUTH = """
import json
from urllib.error import HTTPError
base = os.environ['RUNTIME_BASE_URL'].rstrip('/')
payload = json.loads(urllib.request.urlopen(base+'/health', timeout=10).read())
if payload.get('role') != 'worker' or payload.get('ready') is not True:
    raise RuntimeError('Native Worker is not ready')
# A nonexistent route verifies authentication without loading or mutating a KB.
url = base + '/__workmate_deployment_auth_probe__'
for token, expected in [('', 403), (os.environ['RUNTIME_INTERNAL_TOKEN'], 404)]:
    request = urllib.request.Request(url, headers={'X-Workmate-Internal-Token': token, 'X-User-ID': 'deployment-doctor'})
    try:
        urllib.request.urlopen(request, timeout=10)
    except HTTPError as error:
        if error.code != expected: raise RuntimeError('Worker service authentication failed') from None
    else:
        raise RuntimeError('Worker accepted an unexpected diagnostic route')
"""


def doctor(run, plan, expected_heads=()):
    failures = []
    states = parse_states(run(["ps", "--all", "--format", "json"], quiet=True))
    by_service = {row["Service"]: row for row in states}
    for service in (*plan.dependencies, *plan.apps):
        row = by_service.get(service, {})
        if row.get("State") != "running" or row.get("Health") in (
            "unhealthy",
            "starting",
        ):
            failures.append(service + ": not running or not yet healthy")
    expected = set((*plan.dependencies, *plan.apps, *plan.init_services))
    for row in states:
        if row["Service"] not in expected and row.get("State") == "running":
            print("WARNING running service is not enabled in configuration: " + row["Service"])
    probes = [
        (
            "runtime",
            health_probe("runtime", "http://127.0.0.1:8000/health"),
        ),
        (
            "gateway",
            health_probe("gateway", "http://127.0.0.1:8080/health") + WORKER_AUTH,
        ),
        (
            "gateway",
            "EXPECTED_HEADS = " + repr(list(expected_heads)) + "\n" + DATABASE,
        ),
        ("runtime", DEPENDENCIES),
    ]
    for service, body in probes:
        try:
            run(["exec", "-T", service, "python", "-"], input=guarded(body))
        except RuntimeError as exc:
            failures.append(service + ": " + str(exc))
    try:
        run(
            [
                "exec",
                "-T",
                "frontend",
                "node",
                "-e",
                "fetch('http://127.0.0.1:3000',{signal:AbortSignal.timeout(10000)}).then(r=>{if(!r.ok)process.exit(1);console.log('frontend HTTP OK')}).catch(()=>process.exit(1))",
            ]
        )
    except RuntimeError as exc:
        failures.append("frontend: " + str(exc))
    for failure in failures:
        print("ERROR " + failure)
    print("Diagnostics cover containers, HTTP, internal authentication, and database revision; no model calls or write tests are performed.")
    return not failures


def upgrade_check(run, storage_node_id, limit):
    output = run(['exec', '-T', 'runtime', 'python', '-m',
        'workmate_runtime.operations.upgrade_check', '--storage-node-id', storage_node_id,
        '--limit', str(limit)], accepted_returncodes=(0, 2))
    report = json.loads(output)
    if not isinstance(report, dict):
        raise RuntimeError('Invalid upgrade preflight report; drain or upgrade readiness has not been established')
    state = report.get('drain_state')
    if (report.get('upgrade_authorized') is not False
            or state not in {'candidate_requires_external_verification', 'blocked', 'unknown'}):
        raise RuntimeError('Invalid upgrade preflight report; drain or upgrade readiness has not been established')
    return 0 if state == 'candidate_requires_external_verification' else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--timeout", type=int, default=120)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Check static configuration; --deep validates the full schema using built images")
    check.add_argument("--deep", action="store_true")
    up = sub.add_parser("up", help="Check, build, start configured services, and run diagnostics")
    up.add_argument("--no-build", action="store_true")
    up.add_argument("--dry-run", action="store_true")
    up.add_argument(
        "--skip-security-checks",
        action="store_true",
        help="Accept credential-strength warnings and continue startup; configuration and schema errors still block startup",
    )
    sub.add_parser("status")
    sub.add_parser("doctor")
    upgrade = sub.add_parser('upgrade-check', help='Read-only runtime status report; does not stop admission or authorize an upgrade')
    upgrade.add_argument('--storage-node-id', required=True)
    upgrade.add_argument('--limit', type=int, default=100)
    logs = sub.add_parser("logs")
    logs.add_argument("services", nargs="*")
    logs.add_argument("--tail", type=int, default=100)
    sub.add_parser("stop", help="Stop all project services and preserve data volumes")
    args = parser.parse_args(argv)
    if args.timeout < 1 or (args.command == "logs" and not 1 <= args.tail <= 10000):
        parser.error("timeout must be positive; tail must be between 1 and 10000")
    if args.command == 'upgrade-check' and (
            not args.storage_node_id or len(args.storage_node_id) > 128
            or not 1 <= args.limit <= 1000):
        parser.error('storage-node-id must be nonempty and at most 128 characters; limit must be between 1 and 1000')
    root = args.root.resolve()
    env = args.env_file if args.env_file.is_absolute() else root / args.env_file
    compose_command = [
            "docker",
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(env),
            "-f",
            str(root / "docker-compose.yml"),
        ]
    override = root / "docker-compose.override.yml"
    if override.is_file():
        compose_command.extend(["-f", str(override)])
    compose_command.extend(["--profile", "*"])
    run = Runner(
        compose_command,
        args.timeout,
        getattr(args, "dry_run", False),
    )
    try:
        compose = json.loads(run(["config", "--format", "json"], quiet=True))
        run.redact = Redactor(evaluate({}, compose).secrets)
        if args.command == "status":
            run(["ps", "--all"])
            return 0
        if args.command == "logs":
            if any(s not in compose["services"] for s in args.services):
                raise RuntimeError("Unknown service name")
            run(["logs", "--no-color", "--tail", str(args.tail), *args.services])
            return 0
        if args.command == "stop":
            run(["stop"], mutation=True)
            return 0
        if args.command == 'upgrade-check':
            return upgrade_check(run, args.storage_node_id, args.limit)
        import yaml

        try:
            app = yaml.safe_load((root / "config/workmate.yaml").read_text())
        except yaml.YAMLError as exc:
            raise RuntimeError(
                "Invalid YAML syntax in config/workmate.yaml (source omitted to protect secrets)"
            ) from exc
        plan = evaluate(app, compose)
        run.redact = Redactor(plan.secrets)
        for finding in plan.findings:
            print(f"{finding.level.upper()} {finding.code}: {finding.message}")
        print(
            "Planned services: "
            + ", ".join((*plan.dependencies, *plan.init_services, *plan.apps))
        )
        invalid = any(f.level == "error" for f in plan.findings)
        if args.command == "doctor":
            return 0 if doctor(run, plan, source_heads(root)) and not invalid else 1
        if invalid:
            return 1
        if args.command == "check":
            if args.deep:
                schema_check(run)
            else:
                print(
                    "Static checks passed; run check --deep for full schema validation (requires built images)."
                )
            return 0
        if any(f.security for f in plan.findings) and not args.dry_run:
            if args.skip_security_checks:
                print("Credential-strength warnings were explicitly accepted; configuration checks remain enforced.")
            elif not sys.stdin.isatty():
                print(
                    "Credential-strength warnings remain. To accept them and continue, use up --skip-security-checks."
                )
                return 1
            else:
                try:
                    answer = (
                        input("Credential-strength warnings remain. Accept them and continue startup? [y/N] ")
                        .strip()
                        .lower()
                    )
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                if answer not in ("y", "yes"):
                    print("Startup cancelled.")
                    return 1
        if not args.no_build:
            run(["build", *plan.apps], mutation=True, timeout=1800)
        schema_check(run)
        launch(run, plan, args.timeout, ready=readiness)
        return 0 if args.dry_run or doctor(run, plan, source_heads(root)) else 1
    except ImportError:
        print(
            "ERROR PyYAML is missing: python3 -m pip install -r scripts/requirements.txt",
            file=sys.stderr,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print("ERROR " + run.redact(str(exc)), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
