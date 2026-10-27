# Repository instructions

## Read first

Read the relevant [English documentation](docs/en/README.md) or [Chinese documentation](docs/zh-CN/README.md) before making nontrivial changes. Architecture, configuration, and task recovery contracts are documented there. Code and tests establish current behavior; pinned dependency source establishes upstream contracts.

Documentation describes the current product, not internal discussions or implementation logs.

## Architecture

- AgentScope is pinned to **2.0.7**. Worker drives Core directly; do not add an App HTTP execution chain.
- Frontend accesses only the product API. Task maps to Reply; `tasks.run_id` stores `reply_id`. Do not introduce a Product Run entity.
- PostgreSQL owns authorization and execution authority. Redis Streams transports commands and realtime bodies.
- Preserve owner/epoch/lease checks and durable cleanup evidence. Lease expiry alone does not authorize takeover.
- Approval resume retains Task/Reply identity. Failure recovery creates a new Task/Session. Keep these recovery paths distinct.
- Management, caller use, target-Scope binding, and external identity remain separate and require independent checks.
- Unattended execution rejects actions requiring interactive approval; it must not wait for approval.
- Applications own transactions; Repositories encapsulate SQL and locks. Repositories must not create or commit caller transactions.
- Preserve audit, transport, and browser replay ledgers as distinct responsibilities; do not merge them indiscriminately.

## Workflow

Inspect Git status and preserve unrelated edits. Use focused changes and tests for relevant success, denial, failure, and concurrency paths. Do not modify dependency checkouts or silently upgrade pinned dependencies. Do not delete data volumes or replace real validation with mocked success.

## Configuration and documentation

Policy belongs in `config/workmate.yaml`; secrets and deployment connections belong in environment variables. Add adjacent English comments for each YAML field. Update schemas, both language guides, parameter inventories, and relevant default/loading/override/invalid-value tests together. Update `.env.example` and Compose when deployment variables change. Do not forward unrelated secrets wholesale to services.

Keep repository instruction files and configuration comments in English. Use English for Python comments, docstrings, CLI diagnostics, and ordinary test examples. Preserve localized product messages and fixtures that verify Unicode, multilingual search, or localized output. Keep English and Chinese documentation filenames and navigation aligned. Do not include private hostnames, credentials, personal records, or development timelines in public docs. Preserve third-party licenses. Report only checks actually completed.
