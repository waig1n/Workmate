#!/usr/bin/env python3
"""Synchronize Docker images to a loopback-only remote Registry over SSH.

The remote Docker daemon is first used to seed the Registry with its current
image layers. Local ``docker push`` then uploads only blobs missing from that
Registry. The tool updates remote image tags but never restarts containers.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from urllib.request import urlopen


IMAGE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:\[\]-]*$")
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class ImageSpec:
    local_ref: str
    registry_ref: str


DEFAULT_IMAGES = (
    ImageSpec(
        "workmate-runtime:2.0",
        "workmate-runtime:2.0",
    ),
    ImageSpec("workmate-gateway:2.0", "workmate-gateway:2.0"),
    ImageSpec("workmate-frontend:2.0", "workmate-frontend:2.0"),
)


class SyncError(RuntimeError):
    """A user-actionable synchronization failure."""


def validate_image_ref(value: str) -> str:
    if not IMAGE_REF_PATTERN.fullmatch(value):
        raise ValueError(f"invalid image reference: {value!r}")
    return value


def parse_image_spec(value: str) -> ImageSpec:
    if "=" not in value:
        raise ValueError("image mapping must use LOCAL=REGISTRY")
    local_ref, registry_ref = value.split("=", 1)
    if not local_ref or not registry_ref:
        raise ValueError("image mapping must use non-empty LOCAL=REGISTRY")
    return ImageSpec(
        validate_image_ref(local_ref),
        validate_image_ref(registry_ref),
    )


def validate_remote(value: str) -> str:
    if not REMOTE_PATTERN.fullmatch(value) or value.startswith("-"):
        raise ValueError(f"invalid SSH target: {value!r}")
    return value


def validate_name(value: str, label: str) -> str:
    if not NAME_PATTERN.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def validate_port(value: int) -> int:
    if not 1 <= value <= 65535:
        raise ValueError(f"port must be between 1 and 65535: {value}")
    return value


def registry_binding_is_safe(bindings_json: str, port: int) -> bool:
    try:
        bindings = json.loads(bindings_json)
        entries = bindings.get("5000/tcp") if bindings else None
    except (AttributeError, json.JSONDecodeError):
        return False
    return bool(entries) and all(
        entry.get("HostIp") in {"127.0.0.1", "::1"}
        and entry.get("HostPort") == str(port)
        for entry in entries
    )


def build_sync_plan(
    specs: Sequence[ImageSpec],
    local_ids: dict[str, str],
    remote_ids: dict[str, str],
) -> list[ImageSpec]:
    return [
        spec
        for spec in specs
        if local_ids[spec.local_ref] != remote_ids.get(spec.local_ref)
    ]


class CommandRunner:
    def __init__(self, remote: str, connect_timeout: int) -> None:
        self.remote = validate_remote(remote)
        self.connect_timeout = connect_timeout

    def local(
        self,
        command: Sequence[str],
        *,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            check=check,
            text=True,
            capture_output=capture,
        )

    def remote_command(
        self,
        command: Sequence[str],
        *,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self.local(
            [
                "ssh",
                "-o",
                f"ConnectTimeout={self.connect_timeout}",
                self.remote,
                shlex.join(command),
            ],
            capture=capture,
            check=check,
        )


def require_commands(commands: Sequence[str]) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise SyncError("required commands not found: " + ", ".join(missing))


def inspect_image_id(
    runner: CommandRunner,
    image_ref: str,
    *,
    remote: bool,
    missing_ok: bool = False,
) -> str | None:
    command = ["docker", "image", "inspect", image_ref, "--format", "{{.Id}}"]
    result = (
        runner.remote_command(command, capture=True, check=False)
        if remote
        else runner.local(command, capture=True, check=False)
    )
    if result.returncode != 0:
        if missing_ok:
            return None
        location = "remote" if remote else "local"
        detail = result.stderr.strip()
        raise SyncError(f"{location} image is unavailable: {image_ref}: {detail}")
    return result.stdout.strip()


def inspect_layers(
    runner: CommandRunner,
    image_ref: str,
    *,
    remote: bool,
) -> list[str]:
    command = [
        "docker",
        "image",
        "inspect",
        image_ref,
        "--format",
        "{{json .RootFS.Layers}}",
    ]
    result = (
        runner.remote_command(command, capture=True)
        if remote
        else runner.local(command, capture=True)
    )
    return list(json.loads(result.stdout))


def inspect_all_ids(
    runner: CommandRunner,
    specs: Sequence[ImageSpec],
    *,
    remote: bool,
    missing_ok: bool,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for spec in specs:
        image_id = inspect_image_id(
            runner,
            spec.local_ref,
            remote=remote,
            missing_ok=missing_ok,
        )
        if image_id is not None:
            result[spec.local_ref] = image_id
    return result


def verify_target_platform(runner: CommandRunner, specs: Sequence[ImageSpec]) -> None:
    """This tool applies images to a daemon, not merely to Registry storage."""
    target = runner.remote_command(
        ['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}'], capture=True,
    ).stdout.strip()
    aliases = {'linux/x86_64': 'linux/amd64', 'linux/aarch64': 'linux/arm64'}
    target = aliases.get(target, target)
    for spec in specs:
        platform = runner.local(
            ['docker', 'image', 'inspect', spec.local_ref, '--format', '{{.Os}}/{{.Architecture}}'],
            capture=True,
        ).stdout.strip()
        if platform != target:
            raise SyncError(f'image {spec.local_ref} is {platform}, target daemon is {target}; '
                            'use Registry-only publish for cross-architecture storage')


def print_comparison(
    runner: CommandRunner,
    specs: Sequence[ImageSpec],
    local_ids: dict[str, str],
    remote_ids: dict[str, str],
) -> None:
    for spec in specs:
        local_id = local_ids[spec.local_ref]
        remote_id = remote_ids.get(spec.local_ref)
        if local_id == remote_id:
            layers = inspect_layers(runner, spec.local_ref, remote=False)
            print(f"SAME {spec.local_ref} id={local_id[7:19]} layers={len(layers)}")
            continue
        local_layers = inspect_layers(runner, spec.local_ref, remote=False)
        remote_layers = (
            inspect_layers(runner, spec.local_ref, remote=True)
            if remote_id is not None
            else []
        )
        shared = len(set(local_layers).intersection(remote_layers))
        remote_short = remote_id[7:19] if remote_id else "missing"
        print(
            f"DIFF {spec.local_ref} local={local_id[7:19]} "
            f"remote={remote_short} shared_layers={shared}/{len(local_layers)}",
        )


def ensure_remote_registry(
    runner: CommandRunner,
    *,
    container_name: str,
    registry_image: str,
    port: int,
) -> None:
    inspect = runner.remote_command(
        [
            "docker",
            "inspect",
            container_name,
            "--format",
            "{{json .HostConfig.PortBindings}}|{{.State.Running}}",
        ],
        capture=True,
        check=False,
    )
    if inspect.returncode != 0:
        runner.remote_command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "--restart",
                "unless-stopped",
                "-p",
                f"127.0.0.1:{port}:5000",
                "-v",
                f"{container_name}-data:/var/lib/registry",
                registry_image,
            ],
        )
        return
    bindings, running = inspect.stdout.strip().rsplit("|", 1)
    if not registry_binding_is_safe(bindings, port):
        raise SyncError(
            f"existing Registry {container_name!r} is not bound only to "
            f"loopback port {port}",
        )
    if running != "true":
        runner.remote_command(["docker", "start", container_name])


def local_port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@contextmanager
def registry_tunnel(
    runner: CommandRunner,
    *,
    local_port: int,
    remote_port: int,
    ready_timeout: float = 15.0,
) -> Iterator[None]:
    if not local_port_is_free(local_port):
        raise SyncError(f"local tunnel port is already in use: {local_port}")
    process = subprocess.Popen(
        [
            "ssh",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            f"ConnectTimeout={runner.connect_timeout}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-N",
            "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            runner.remote,
        ],
    )
    try:
        deadline = time.monotonic() + ready_timeout
        registry_url = f"http://127.0.0.1:{local_port}/v2/"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SyncError("SSH Registry tunnel exited before becoming ready")
            try:
                with urlopen(registry_url, timeout=1) as response:  # noqa: S310
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.2)
        else:
            raise SyncError("timed out waiting for the Registry SSH tunnel")
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def sync_changed_images(
    runner: CommandRunner,
    specs: Sequence[ImageSpec],
    remote_ids: dict[str, str],
    *,
    local_port: int,
    remote_port: int,
    keep_local_tags: bool,
) -> None:
    for spec in specs:
        if spec.local_ref not in remote_ids:
            continue
        baseline_ref = f"localhost:{remote_port}/{spec.registry_ref}"
        print(f"SEED {spec.local_ref} -> {baseline_ref}")
        runner.remote_command(["docker", "tag", spec.local_ref, baseline_ref])
        runner.remote_command(["docker", "push", baseline_ref])

    temporary_refs: list[str] = []
    try:
        with registry_tunnel(
            runner,
            local_port=local_port,
            remote_port=remote_port,
        ):
            for spec in specs:
                tunnel_ref = f"127.0.0.1:{local_port}/{spec.registry_ref}"
                temporary_refs.append(tunnel_ref)
                print(f"PUSH {spec.local_ref} -> {tunnel_ref}")
                runner.local(["docker", "tag", spec.local_ref, tunnel_ref])
                runner.local(["docker", "push", tunnel_ref])

        for spec in specs:
            registry_ref = f"localhost:{remote_port}/{spec.registry_ref}"
            print(f"APPLY {registry_ref} -> {spec.local_ref}")
            runner.remote_command(["docker", "pull", registry_ref])
            runner.remote_command(["docker", "tag", registry_ref, spec.local_ref])
    finally:
        if not keep_local_tags:
            for image_ref in temporary_refs:
                runner.local(
                    ["docker", "image", "rm", image_ref],
                    capture=True,
                    check=False,
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare and incrementally synchronize workmate Docker images "
            "through a loopback-only remote Registry and SSH tunnel. "
            "For independent image cleanup, use: cleanup --help."
        ),
    )
    parser.add_argument("--remote", required=True, help="Explicit SSH target (user@host)")
    parser.add_argument(
        "--image",
        action="append",
        metavar="LOCAL=REGISTRY",
        help="image mapping; repeat to replace the three workmate defaults",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare image IDs/layers without changing local or remote state",
    )
    parser.add_argument("--local-port", type=int, default=5001)
    parser.add_argument("--remote-registry-port", type=int, default=5000)
    parser.add_argument("--registry-container", default="workmate-registry")
    parser.add_argument("--registry-image", default="registry:2.8.3")
    parser.add_argument("--connect-timeout", type=int, default=10)
    parser.add_argument("--keep-local-tags", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "cleanup":
        if __package__:
            from .image_cleanup import main as cleanup_main
        else:
            from image_cleanup import main as cleanup_main
        return cleanup_main(argv[1:], CommandRunner)
    if argv and argv[0] == "sync":
        argv = argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        require_commands(("docker", "ssh"))
        remote = validate_remote(args.remote)
        local_port = validate_port(args.local_port)
        remote_port = validate_port(args.remote_registry_port)
        container_name = validate_name(
            args.registry_container,
            "Registry container name",
        )
        registry_image = validate_image_ref(args.registry_image)
        specs = (
            tuple(parse_image_spec(value) for value in args.image)
            if args.image
            else DEFAULT_IMAGES
        )
        runner = CommandRunner(remote, args.connect_timeout)
        verify_target_platform(runner, specs)
        local_ids = inspect_all_ids(
            runner,
            specs,
            remote=False,
            missing_ok=False,
        )
        remote_ids = inspect_all_ids(
            runner,
            specs,
            remote=True,
            missing_ok=True,
        )
        print_comparison(runner, specs, local_ids, remote_ids)
        changed = build_sync_plan(specs, local_ids, remote_ids)
        if args.check or not changed:
            print("No upload required." if not changed else "Check only; no images uploaded.")
            return 0

        ensure_remote_registry(
            runner,
            container_name=container_name,
            registry_image=registry_image,
            port=remote_port,
        )
        sync_changed_images(
            runner,
            changed,
            remote_ids,
            local_port=local_port,
            remote_port=remote_port,
            keep_local_tags=args.keep_local_tags,
        )
        verified_ids = inspect_all_ids(
            runner,
            changed,
            remote=True,
            missing_ok=False,
        )
        mismatched = [
            spec.local_ref
            for spec in changed
            if verified_ids.get(spec.local_ref) != local_ids[spec.local_ref]
        ]
        if mismatched:
            raise SyncError("remote verification failed: " + ", ".join(mismatched))
        print("Synchronization complete; application containers were not restarted.")
        return 0
    except (OSError, subprocess.CalledProcessError, SyncError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
