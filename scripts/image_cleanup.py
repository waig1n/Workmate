"""Conservative, explicit cleanup of local or remote Workmate image tags."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
import sys


OWNED_TAG = re.compile(
    r"^(?:(?:localhost|127\.0\.0\.1):[0-9]+/)?"
    r"workmate-(?:gateway|runtime|frontend):[A-Za-z0-9_][A-Za-z0-9_.-]*$"
)
OWNED_REPOSITORY = re.compile(
    r"^(?:(?:localhost|127\.0\.0\.1):[0-9]+/)?"
    r"workmate-(?:gateway|runtime|frontend)$"
)
IMAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")


@dataclass(frozen=True)
class Image:
    id: str
    tags: tuple[str, ...]
    created: datetime


@dataclass(frozen=True)
class State:
    images: tuple[Image, ...]
    protected_ids: frozenset[str]
    protected_refs: frozenset[str]


@dataclass(frozen=True)
class Decision:
    image: Image
    candidate: bool
    reason: str
    tags: tuple[str, ...]


def build_plan(state: State, older_than_days: int, now: datetime | None = None,
               *, images=(), tags=()) -> list[Decision]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=older_than_days)
    decisions = []
    selected = bool(images or tags)
    for image in sorted(state.images, key=lambda item: item.tags):
        if not any(OWNED_TAG.fullmatch(tag) for tag in image.tags):
            continue
        selected_tags = tuple(ref for ref in image.tags if OWNED_TAG.fullmatch(ref)
                              and (not selected or ref in images or ref.rsplit(":", 1)[1] in tags))
        if not selected_tags:
            continue
        if image.id in state.protected_ids or set(image.tags) & state.protected_refs:
            reason = "referenced by container, Compose or --keep"
        elif any(not OWNED_TAG.fullmatch(tag) for tag in image.tags):
            reason = "has an alias outside the cleanup scope"
        elif not selected and image.created >= cutoff:
            reason = "within retention window"
        else:
            decisions.append(Decision(image, True, "explicitly selected and unreferenced" if selected else "old and unreferenced", selected_tags))
            continue
        decisions.append(Decision(image, False, reason, selected_tags))
    return decisions


def _run(run, command):
    result = run(command, capture=True, check=False)
    if result.returncode:
        # Never echo Compose output: interpolation errors may contain secrets.
        raise ValueError("Docker/SSH command failed; cleanup stopped (output withheld)")
    return result.stdout


def _inspect_ref(run, ref: str, *, missing_ok: bool) -> str | None:
    result = run(["docker", "image", "inspect", ref, "--format", "{{.Id}}"],
                 capture=True, check=False)
    if result.returncode:
        # Accept only Docker's precise missing-image diagnostic, not daemon/SSH errors.
        missing = result.stderr.strip() in {
            f"Error response from daemon: No such image: {ref}",
            f"Error: No such image: {ref}",
        }
        if missing_ok and missing:
            return None
        raise ValueError("Required image inspection failed; cleanup stopped (output withheld)")
    value = result.stdout.strip()
    if not value.startswith("sha256:"):
        raise ValueError("Unexpected Docker image identity; cleanup stopped")
    return value


def collect_state(run, compose_dir: str, keep: list[str]) -> State:
    ids = sorted(set(_run(run, ["docker", "image", "ls", "-aq", "--no-trunc"]).split()))
    images = []
    identities = {}
    # Bound request sizes while avoiding one SSH round trip per image.
    for offset in range(0, len(ids), 100):
        batch = ids[offset:offset + 100]
        rows = json.loads(_run(run, ["docker", "image", "inspect", *batch]))
        if len(rows) != len(batch):
            raise ValueError("Incomplete Docker image snapshot; cleanup stopped")
        for row in rows:
            tags = tuple(sorted(row.get("RepoTags") or ()))
            # Digests are also aliases: never delete externally named images accidentally.
            digests = row.get("RepoDigests") or ()
            for ref in (*tags, *digests, row["Id"]):
                identities[ref] = row["Id"]
            for digest in digests:
                repository = digest.split("@", 1)[0]
                if not OWNED_REPOSITORY.fullmatch(repository):
                    tags += (digest,)
            created = datetime.fromisoformat(row["Created"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                raise ValueError("Docker image timestamp has no timezone")
            images.append(Image(row["Id"], tags, created))
    protected = set()
    containers = _run(run, ["docker", "ps", "-aq"]).split()
    for offset in range(0, len(containers), 100):
        batch = containers[offset:offset + 100]
        image_ids = _run(run, ["docker", "container", "inspect", *batch,
                               "--format", "{{.Image}}"]).splitlines()
        if len(image_ids) != len(batch) or any(
            not image_id.startswith("sha256:") for image_id in image_ids
        ):
            raise ValueError("Unexpected container image identities; cleanup stopped")
        protected.update(image_ids)
    refs = set(_run(run, ["docker", "compose", "--project-directory", compose_dir,
                          "config", "--images"]).split())
    if not refs:
        raise ValueError("Compose returned no images; cleanup stopped")
    for ref in sorted(refs | set(keep)):
        image_id = identities.get(ref) or identities.get(ref + ":latest")
        if not image_id:
            # Docker normalizes docker.io/library aliases and other equivalent refs.
            image_id = _inspect_ref(run, ref, missing_ok=ref not in keep)
        if image_id:
            protected.add(image_id)
    return State(tuple(images), frozenset(protected), frozenset(refs | set(keep)))


def execute_cleanup(run, args) -> int:
    state = collect_state(run, args.compose_dir, args.keep)
    available = {ref for image in state.images for ref in image.tags}
    if set(args.image) - available:
        raise ValueError("An explicitly selected image tag is missing; cleanup stopped")
    plan = build_plan(state, args.older_than_days, images=args.image, tags=args.tag)
    for decision in plan:
        action = "CANDIDATE" if decision.candidate else "KEEP"
        print(f"{action} {', '.join(decision.tags)} [{decision.image.id}] {decision.reason}")
    candidates = [(decision.image.id, tag) for decision in plan if decision.candidate
                  for tag in decision.tags]
    if not args.apply:
        print(f"Dry run: {len(candidates)} tag(s) eligible; use --apply to delete.")
        return 0
    removed = 0
    for image_id, tag in candidates:
        # Re-read protections for every tag. Never expand the original candidate set.
        fresh = build_plan(collect_state(run, args.compose_dir, args.keep), args.older_than_days,
                           images=args.image, tags=args.tag)
        eligible = {(decision.image.id, ref) for decision in fresh if decision.candidate
                    for ref in decision.tags}
        if (image_id, tag) not in eligible:
            print(f"SKIP {tag}: state or protections changed")
            continue
        if _inspect_ref(run, tag, missing_ok=True) != image_id:
            print(f"SKIP {tag}: image identity changed")
            continue
        _run(run, ["docker", "image", "rm", tag])
        print(f"REMOVED {tag}")
        removed += 1
    print(f"Removed {removed} tag(s). Volumes, build cache and Registry storage were not touched.")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("local", "remote"), required=True)
    parser.add_argument("--remote", help="SSH target required for --target remote")
    parser.add_argument("--compose-dir", required=True, help="Compose directory on the target machine")
    parser.add_argument("--keep", action="append", default=[], metavar="IMAGE")
    parser.add_argument("--image", action="append", default=[], metavar="IMAGE:TAG",
                        help="select an exact Workmate image tag; repeatable, bypasses age filtering")
    parser.add_argument("--tag", action="append", default=[], metavar="TAG",
                        help="select this exact tag across Workmate repositories; repeatable")
    parser.add_argument("--older-than-days", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.target == 'remote' and not args.remote:
        parser.error('--remote is required for a remote target')
    if (args.image or args.tag) and args.older_than_days is not None:
        parser.error("--image/--tag cannot be combined with --older-than-days")
    if any(not OWNED_TAG.fullmatch(ref) for ref in args.image):
        parser.error("--image must be an exact Workmate repository:tag (no wildcard or digest)")
    if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", tag) for tag in args.tag):
        parser.error("--tag must be an exact tag, without wildcard or repository")
    if args.older_than_days is None:
        args.older_than_days = 7
    if not 0 <= args.older_than_days <= 365000:
        parser.error("--older-than-days must be between 0 and 365000")
    if not args.compose_dir.strip():
        parser.error("--compose-dir must not be empty")
    if any(not IMAGE_REF.fullmatch(ref) for ref in args.keep):
        parser.error("--keep must be a valid image reference")
    return args


def main(argv, runner_factory) -> int:
    args = parse_args(argv)
    try:
        runner = runner_factory(args.remote or 'localhost', 10)
        run = runner.remote_command if args.target == "remote" else runner.local
        return execute_cleanup(run, args)
    except (ValueError, KeyError, IndexError, TypeError, OSError, subprocess.SubprocessError):
        print("error: cleanup stopped: invalid configuration/state or Docker/SSH failure; no forced deletion was attempted", file=sys.stderr)
        return 1
