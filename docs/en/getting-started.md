# Getting started

[English](../en/getting-started.md) | [简体中文](../zh-CN/getting-started.md)

Build Workmate on a Linux host, create your team workspace, and run your first task.

## Requirements

Use a Linux host with Docker Compose v2. The default Bubblewrap sandbox requires Linux namespaces; see [deployment](deployment.md) for container permissions. Prepare API credentials for the selected model profiles. Initial builds require access to container registries and package indexes.

Run the commands below from the repository root. The explicit Compose file avoids automatically loading machine-specific override files.

## Configure

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and complete these steps:

1. Set a strong `POSTGRES_PASSWORD` and update the passwords in `DATABASE_URL` and `AS_DATABASE_URL`. URL-encode special characters.
2. Generate separate random values for `JWT_SECRET` and `WORKMATE_INTERNAL_TOKEN`. Replace template placeholders.
3. Set `KNOWLEDGE_S3_ACCESS_KEY` / `KNOWLEDGE_S3_SECRET_KEY` for the local RustFS service. Skill storage needs this service even with knowledge features disabled. Set `SKILL_S3_ACCESS_KEY` / `SKILL_S3_SECRET_KEY` to the matching credentials, using separate Skill and knowledge buckets.
4. Set model credentials and optional gateway URLs. Review the chat, embedding, and ReMe configuration in `config/workmate.yaml`. Model names, types, capabilities, and environment references must match the provider. See [models](models.md) for required profiles.
5. Set `WORKMATE_PUBLIC_URL` to the frontend URL users actually visit. For local use, `http://localhost:3000` is suitable.

Enabled optional features also require their credentials and services. See [integrations](integrations.md) for knowledge and Web setup, and [configuration](configuration.md) for parameters.

The supplied YAML currently enables knowledge and Web. The startup command below enables their services; first set `KNOWLEDGE_QDRANT_API_KEY`, `KNOWLEDGE_EMBEDDING_MODEL_API_KEY`, `SEARXNG_SECRET`, and `WEB_FETCH_API_TOKEN`. For a minimal workspace, set `product.knowledge.enabled` and `product.web.enabled` to `false` before omitting the corresponding profiles.

## Build and start

```bash
docker compose -f docker-compose.yml build gateway runtime frontend
docker compose -f docker-compose.yml --profile knowledge --profile web-tool up -d
docker compose -f docker-compose.yml ps
```

Default endpoints are `http://localhost:3000` for the frontend and `http://localhost:8080/docs` for API documentation. Effective Compose settings determine actual ports. Runtime has no published host port.

After object storage becomes healthy, `storage-init` initializes private buckets. Gateway applies database migrations and becomes healthy before Worker starts. `storage-init` is a one-shot job; successful exit is expected. If initialization or migration fails, resolve it using the troubleshooting steps below.

## First use

1. Open the frontend and create the organization workspace and its first administrator.
2. Create ordinary member accounts and configure departments and roles as needed.
3. Select the default workspace assistant or create a personal Agent, then choose an available model. An administrator configures and shares required Skills and MCP connections.
4. Start a conversation and submit a task. Respond to approval cards when required, inspect tools in Activity, and preview or download outputs in the artifact panel.

See the [user guide](user-guide.md) and [administration](administration.md) for next steps.

## Startup failures

```bash
docker compose -f docker-compose.yml logs --tail=100 gateway runtime storage-init
```

Check missing or inconsistent credentials, model names, object buckets, migrations, and sandbox permissions first. Remove authorization headers, secrets, and user content before sharing logs. Do not delete data volumes to fix initialization failures.
