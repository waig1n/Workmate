# Deployment and operations

[English](../en/deployment.md) | [简体中文](../zh-CN/deployment.md)

Deploy a single-host workspace or expand to multiple Workers. This guide covers service layout, persistence, scaling, updates, and backups.

## Services and isolation

Default deployment includes `gateway`, `runtime`, `frontend`, PostgreSQL, Redis, RustFS, and the one-shot `storage-init` job. Knowledge uses the `knowledge` profile; Web uses `web-tool`. Application images are `workmate-gateway`, `workmate-runtime`, and `workmate-frontend`.

Build from source using the root Compose file; see [getting started](getting-started.md). Default Bubblewrap requires Linux namespaces. The Runtime container currently uses `SYS_ADMIN` and relaxed seccomp/AppArmor settings. Assess host isolation and deployment trust boundaries rather than assuming these permissions are safe everywhere.

Internal addresses `http://gateway:8080` and `http://runtime:8000` work only on the relevant container network. Keep databases, Redis, object storage, and internal Runtime interfaces private. Public ingress should use TLS and support SSE without response caching, with suitable proxy timeouts.

## Persistence

Preserve PostgreSQL, Redis AOF, RustFS, product files, Worker Workspace, and ReMe volumes. Skill caches are rebuildable, but not all Worker data is a cache.

The Compose project name determines volume and network names. Changing it on an existing deployment selects different resources. Retain project and volume identity during updates. Do not use `docker compose down -v` as an upgrade procedure.

## Updating

1. Back up databases, object data, and configuration; record images and effective settings.
2. Stop new admissions, let execution and approval tasks reach a safe maintenance state, and stop writers.
3. Build matching Gateway, Runtime, and Frontend versions and complete database migrations first.
4. Verify Gateway, storage, and initialization jobs before starting Worker and frontend.
5. Check login, conversations, approval, Stop, artifact downloads, and required integrations.

New installations use an empty PostgreSQL database or schema, initialized by `0001_initial_schema`. Initialization rejects nonempty schemas and does not support downgrading to erase initialized data. Stamping a revision does not apply its migration.

The Gateway container entrypoint runs migrations. For source startup, configure connections and run `.venv/bin/alembic upgrade head` from `backend/` first. Updating only the frontend or stamping without applying migrations is insufficient. Rolling back an image after a forward database migration may not restore a compatible system.

## Multiple Workers and machines

Workmate supports separate Gateway and Worker deployments, multiple Worker replicas, and fixed multi-node execution. Start with single-host Compose, then expand Worker resources and plan storage nodes as team concurrency grows. Different conversations can execute concurrently; each conversation remains serial.

Same-node replicas can hand over only when they share the persistent working set and storage identity. Scaling does not automatically create shared workspaces. Check backend capabilities, mounts, ownership, and suspension/resume behavior before adding replicas.

Fixed multi-node deployment requires S3 artifacts and attachments, with a unique `WORKMATE_WORKER_STORAGE_NODE_ID` for each real storage node. Configure `WORKMATE_WORKER_NODES` on Gateway. Roles share databases, Redis, and object storage; internal endpoints must be reachable across hosts. User-and-Agent storage affinity remains fixed.

An offline node does not trigger arbitrary tool replay or transparent takeover. See [tasks and recovery](runtime.md) for explicit checkpoint recovery. For multiple Gateways or hosts, validate concurrency and failure handling under your expected workload and configure monitoring for that topology.

## Backup and troubleshooting

A consistent cold backup requires all writers to remain stopped throughout the backup window and must cover databases and corresponding file/object data. Verify restoration into isolated resources before replacing live data. Do not upload secrets from backups, logs, or configuration to a public repository.

Start troubleshooting with Compose status, Gateway/Worker health, migrations, database connections, Redis capacity, and object-store permissions. Redis uses AOF and `noeviction`; full memory rejects writes. Monitor backlog and disk usage instead of evicting unacknowledged commands.

See [testing and operational tools](tooling.md) for commands and parameters. Remote operations require an explicit target. Read `--help` and prefer read-only previews. Existing deployments must retain their original Compose project name; new installations default to `workmate`.
