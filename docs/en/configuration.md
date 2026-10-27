# Configuration

[English](../en/configuration.md) | [简体中文](../zh-CN/configuration.md)

Configure models, storage, and execution policies for your deployment. Routine settings live in a YAML file and environment variables, without changes to application code.

## Sources

Store non-secret policies in [config/workmate.yaml](../../config/workmate.yaml). Supply connections, secrets, and deployment paths through environment variables; see [.env.example](../../.env.example). `WORKMATE_CONFIG_FILE` selects the YAML path.

Settings precedence is explicit initialization arguments, non-empty environment variables / `.env`, YAML, then code defaults. Default Compose passes only explicitly listed variables. Adding a variable to the host `.env` does not guarantee it reaches a container. Inject advanced overrides into the relevant services explicitly; do not pass a file containing every secret to every role.

The [parameter reference](configuration-reference.md) lists every YAML setting and deployment variable name in the repository templates. Its values are configured template values, not necessarily schema defaults when fields are omitted. Types, enums, ranges, and cross-field constraints are defined in [shared configuration types](../../packages/product/src/workmate_product/config) and [Gateway settings](../../backend/app/config.py).

## Responsibilities

| Area | Configuration and boundary |
|---|---|
| Model profiles | Model name, type, capabilities, context and output limits; reference secrets through `api_key_env` |
| Authentication and registration | JWT secret in the environment; registration mode, expiry policies, and rate limits in YAML |
| Skill storage | `SKILL_S3_*` connects Gateway and Worker to the same private bucket |
| Artifacts and attachments | `product.artifacts.storage` selects the backend; S3 uses a separate `ARTIFACT_S3_*` bucket |
| Knowledge | `product.knowledge.enabled`; dedicated embedding, Qdrant, and source-document storage |
| Worker | Storage-node identity, concurrency, leases, draining, and workspace lifecycle |
| Command queue | Redis connection, namespace, bounded retries, and retention; does not replace PostgreSQL execution authority |
| Realtime events | Body retention, buffers, snapshots, readers, and client budgets; not cluster-wide capacity |
| Sandbox | Backend, resource reclamation, and provider parameters; credentials go only to Worker |
| Schedules and IM | Scheduling policies, channel switches, account environment references, and notification policies |

## Connections and secrets

`DATABASE_URL` connects to the product database; `AS_DATABASE_URL` connects to the Runtime database. Both must target the appropriate deployment databases. Compose maps `WORKMATE_INTERNAL_TOKEN` into the caller and Runtime internal-interface credentials.

`WORKMATE_PUBLIC_URL` is the user-facing frontend URL and accepts HTTP/HTTPS; use HTTPS for public deployments. `PRODUCT_API_URL` is the Next.js server-side Gateway address and must not be used to build user-facing links.

Local RustFS starts with `KNOWLEDGE_S3_*` credentials. The Skill bucket always needs initialization; knowledge and Artifact buckets are initialized according to enabled features. For external S3, provision private buckets and permissions separately. Keep buckets for different data classes separate.

`WORKMATE_WORKER_STORAGE_NODE_ID` identifies stable storage, not an ephemeral process. `WORKMATE_WORKER_NODES` configures a fixed-node directory. Multi-machine deployments also require S3 and reachable nodes; see [deployment](deployment.md).

## Changing settings

Inspect effective configuration and back up before making changes. Model and policy changes may require recreating relevant services. Rebuilding an image does not automatically overwrite user-customized Agents stored in the database.

To add configuration options, see [development and contributing](development.md). Server-side authorization and approval rules remain in force for all configurations.

## Redis, paths, and backend connections

| Environment variables | Purpose |
|---|---|
| `AS_REDIS_URL` | Redis connection used by AgentScope auxiliary components |
| `WORKMATE_COORDINATION_REDIS_URL`, `WORKMATE_COORDINATION_NAMESPACE` | Product command and realtime coordination connection and namespace; keep roles consistent and isolate deployments |
| `ARTIFACT_STORAGE_DIR` | Local artifact storage path; persist it and make it visible to required roles when using the local backend |
| `WORKMATE_WORKER_WORKSPACE_BASEDIR`, `WORKMATE_WORKER_REME_BASEDIR` | Persistent Worker files and long-term memory directories |
| `WORKMATE_SKILL_CACHE_DIR` | Skill materialization cache; not a replacement for the private package bucket |
| `WORKMATE_WORKER_STOP_GRACE_PERIOD` | Compose shutdown grace period; allow enough time for Worker draining and cleanup |
| `DOCKER_HOST` | Docker sandbox connection; explicitly configure host socket mounts |
| `WORKMATE_SANDBOX_KUBECONFIG` | Kubernetes client configuration; empty uses in-cluster identity, file-based configuration needs an explicit read-only mount |
| `E2B_API_KEY`, `E2B_DOMAIN` | E2B credentials and optional domain, for Worker only |
| `DAYTONA_API_KEY`, `DAYTONA_API_URL` | Daytona credentials and optional service URL |
| `OPEN_SANDBOX_API_KEY`, `OPEN_SANDBOX_DOMAIN` | OpenSandbox credentials and endpoint |
| `WEB_SEARCH_BASE_URL`, `WEB_FETCH_BASE_URL` | Web service addresses reachable from Worker, not browser-facing addresses |
| `PIP_INDEX_URL`, `PIP_DEFAULT_TIMEOUT` | Build-time package index and download timeout, not model-call timeout |

Changing a namespace, directory, or node identity does not migrate data. Check existing commands, object references, working directories, and ownership before treating a new location as existing state. Supply backend credentials only to roles that use them, never to Agent tool environments.
