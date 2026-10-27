# Development and contributing

[English](../en/development.md) | [简体中文](../zh-CN/development.md)

Contributions are welcome: new features, bug fixes, documentation, and integrations. This guide covers environment setup, code locations, testing, and preparing a reviewable change.

## Environment

Python requires version 3.12 or a compatible newer version. Use `uv` for Python dependencies and npm with the repository lockfile for frontend dependencies. Running services also requires PostgreSQL, Redis, and object storage configuration. Install the AgentScope release specified by the lockfile.

From the repository root:

```bash
(cd backend && uv sync --extra dev)
(cd runtime && uv sync --extra dev)
(cd frontend && npm ci)
```

Submit dependency upgrades as explicit changes, with the relevant package declarations and lockfiles updated together.

## Validation

```bash
backend/.venv/bin/python -m pytest backend/tests/test_document_governance.py -q --noconftest
(cd backend && .venv/bin/python -m pytest tests -q)
(cd runtime && .venv/bin/python -m pytest tests -q)
(cd frontend && npm test && npm run typecheck)
```

Some integration tests require external services and are skipped when those services are not configured. Browser tests use Playwright; see [frontend](../../frontend). Use an isolated environment for tests that create services or write data, and report both completed checks and skipped coverage.

## Change locations

| Change | Main location and constraints |
|---|---|
| Product API | `backend/app`; recheck authorization and target Scope on the server |
| Business logic and persistence | `packages/product`; applications orchestrate transactions, Repositories own SQL/locks |
| Execution and tools | `runtime/workmate_runtime`; preserve owner/epoch, approval, and cleanup evidence |
| Shared protocols | `packages/contracts`; update producers, consumers, and contract tests together |
| Workspace UI | `frontend`; product API only, with SSE reset and authorized downloads |
| Database | `backend/alembic`; start with `0001_initial_schema`; add new forward revisions after release |
| Configuration | Update schemas, YAML comments, environment templates, Compose, and both language guides |

Test successful, denied, and failing paths. Authorization, concurrency, approval, and recovery changes require boundary-specific coverage. For protocol and persistence changes, assert actual state, event ordering, and stored results.

## Documentation

`README.md` is the English homepage; `README.zh-CN.md` is the Chinese homepage. `docs/en/` and `docs/zh-CN/` use matching filenames and counterpart navigation. Both languages describe the same features, constraints, and configuration. Update both when changing either.

Organize documentation around reader tasks: user guides explain workflows, while architecture and contributor guides explain implementation contracts. Describe implemented behavior. Write natural Chinese and idiomatic English rather than translating sentence structure literally, while keeping features, settings, and limitations aligned.

Code, schemas, and tests establish current behavior; pinned dependency source establishes upstream contracts. See [configuration](configuration.md) and the [documentation index](README.md).

## Submit a contribution

Keep changes focused and preserve third-party licenses and attribution. Review the diff before submission; credentials, logs, and database backups belong outside source control.

Describe the problem, resulting behavior, and test results. Include screenshots for UI changes when useful. Configuration changes require matching schemas, English YAML comments, bilingual references, and tests for defaults, loading, overrides, and invalid values. New deployment variables also require updates to `.env.example` and Compose.
