# Testing and operational tools

[English](../en/tooling.md) | [简体中文](../zh-CN/tooling.md)

The repository includes deployment, regression, load-testing, fault-injection, and release tools. Choose tools for your target environment and preview operations before creating or changing resources.

## Layout and responsibilities

| Location or entry | Purpose |
|---|---|
| `backend/tests`, `runtime/tests`, `packages/product/tests` | Product, execution-protocol, and shared-business regressions |
| `frontend/e2e` | Browser behavior and screenshot regression tests using generic sample data |
| `scripts/tests` | Operational argument, safety-boundary, and orchestration unit tests |
| `scripts/reliability/load.py` | Deterministic mixed workloads in isolated local infrastructure |
| `scripts/reliability/boundary_drill.py`, `database_tool_drill.py` | Execution-boundary crashes and database faults during tool execution |
| `scripts/reliability/dependency_drill.py`, `s3_http_fault_drill.py` | Dependency and object-storage fault injection |
| `scripts/reliability/capacity_support`, `soak_support` | Capacity and sustained-load clients, state and streaming consistency checks |
| `scripts/reliability/remote_fixture.py` | Provisioning, status, collection, and cleanup of isolated resources on an explicit target |
| `scripts/reliability/cold_backup.py`, `restore_drill.py` | Cold backup and isolated restoration verification |
| `scripts/release` | Build, image audit, packaging, and release tooling |

The database starts at `0001_initial_schema`. Regression tests cover database migrations and Core state-format compatibility.

## Local tests

After installing development dependencies, run from the repository root. Use the Runtime virtual environment for tool tests to avoid the backend's separate `scripts` package:

```bash
runtime/.venv/bin/python -m pytest scripts/tests -q --noconftest
runtime/.venv/bin/python -m scripts.reliability.load --runtime-image workmate-runtime:2.0 --gateway-image workmate-gateway:2.0 --workload mixed
```

The second command only prints a plan. Add `--run` to create isolated Docker resources. Build matching-architecture images first and provide Linux namespaces and required container permissions. Tests skipped for missing external services do not establish integration acceptance.

`baseline.py` samples only the local host by default; pass `--remote user@host` to sample a remote host. Its health probe uses `http://127.0.0.1:8080/health` on the sampled machine. Use `--help` for capacity and sustained-workload parameters.

## Remote tests

```bash
runtime/.venv/bin/python -m scripts.reliability.remote_fixture provision --remote operator@example.test --runtime-image workmate-runtime:2.0 --gateway-image workmate-gateway:2.0 --output /tmp/workmate-capacity
```

Replace the example host, inspect the plan, then add `--run` to execute. Runtime and infrastructure images must exist in the target Docker daemon; the Gateway dependency image must exist locally. The tool resolves image IDs before execution. SSH and noninteractive remote sudo/Docker access are required.

An explicitly set `WORKMATE_TEST_SSH_TARGET` can replace `--remote`; no target host is built in. State files record the host and resource owner. Subsequent `status`, `collect`, and `cleanup` actions require `--state` and the matching target. Capacity and sustained-workload orchestration propagates that recorded target to monitoring subprocesses.

Cleanup applies only to verified test resources and preserves existing application containers. Store reports and logs in a separate output directory, and record the target environment, image versions, and workload with remote test results.

## Deployment and dependency maintenance

```bash
./scripts/workmate check
./scripts/workmate up --dry-run
./deploy/start.sh
```

Deployment uses the root `.env.example`, `config/workmate.yaml`, and `docker-compose.yml`. `deploy/start.sh` calls the same deployment tool, which selects services from configuration. Complete [getting started](getting-started.md) first. New installations default to project name `workmate`; existing installations must explicitly retain their original project name and volumes.

Machine-specific `docker-compose.override.yml`, image-version overrides, and generated working material are not versioned. Maintain overrides in the deployment environment when needed.

```bash
python3 scripts/sync_images_to_remote.py --remote operator@example.test --check
python3 scripts/runtime_install.py --target local --project workmate --apt poppler-utils
python3 scripts/runtime_install.py --target remote --remote operator@example.test --project workmate --apt poppler-utils
python3 scripts/sync_images_to_remote.py cleanup --target remote --remote operator@example.test --compose-dir /srv/workmate
```

Hot installation and image cleanup preview changes by default; add `--apply` to execute. Image synchronization uses `--check` for previews and executes when that flag is omitted. Hot installation affects current containers and disappears on recreation. Put persistent dependencies into the Runtime image. Always verify the target, project, resource identities, and tool output.

## Release packages

`python -m scripts.release --help` lists check, build, verify, bundle, publish, and import commands. Python images preserve application source. Builds record source hashes and include project and third-party notices; verification imports installed modules, checks operational entrypoints, and audits every image layer and nested archives for environment files and known private-key markers. This does not replace a full secret scan of the intended Git snapshot.

`verify` without an acceptance report checks packaging only. `bundle` and `publish` also require a clean production build and acceptance evidence bound to the exact image IDs. The application SBOM records available dependency license metadata and does not cover operating-system packages or independently supplied infrastructure images. Review [third-party sources](third-party.md) before redistribution. Publish is explicit and is never a side effect of build or verify.
