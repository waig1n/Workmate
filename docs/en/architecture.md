# Architecture

[English](../en/architecture.md) | [简体中文](../zh-CN/architecture.md)

Workmate separates product services from Agent execution. AgentScope 2.0.7 powers the execution core, while Workmate provides shared access control, task management, and file delivery for teams.

![Workmate architecture: enterprise authorization, durable execution, and artifact delivery](../assets/diagrams/system-overview-en.svg)

The diagram separates ingress, product and execution services, capability integrations, and shared infrastructure. PostgreSQL, Redis Streams, S3-compatible object storage, persistent volumes, and optional Qdrant belong to the infrastructure group. Links show dependencies; each service accesses the stores required by its role. Browsers access only the product API.

Gateway persists the Task and outbox intent in one transaction; an application-side publisher sends commands to Redis Streams. Workers consume commands, then claim and verify execution authority in PostgreSQL. Object storage holds Skill packages and knowledge source files. Attachments and artifacts default to local persistent storage and can use S3; file checkpoints require separate enablement.

## Components

| Location | Responsibility |
|---|---|
| `frontend/` | Next.js workspace; communicates only with the product API |
| `backend/` | FastAPI Gateway, authorization, Task admission, event projection, and Alembic |
| `runtime/` | Native Worker assembling and driving AgentScope Core directly |
| `packages/contracts/` | Shared protocol types |
| `packages/product/` | Shared business logic, persistence, configuration, and resource governance |

Gateway starts at `app.main:app`; Worker starts at `workmate_runtime.roles.worker`. Browsers do not connect directly to AgentScope. Workers receive tasks through the command queue.

## Distributed execution and horizontal scaling

Workmate separates the product entry point from Agent execution so teams can expand execution resources as workloads grow, while retaining one authorization model, task history, and artifact access layer.

- **Multiple Workers serve concurrent tasks.** Redis Streams delivers commands; Workers claim and verify execution authority in PostgreSQL. Additional replicas expand processing concurrency across conversations, while each conversation remains serial.
- **Shared state with explicit ownership.** Tasks, control intents, and execution evidence persist in shared services. Owner / epoch / lease checks constrain duplicate delivery and stale Workers. Adding processes does not change authorization boundaries.
- **Scale with the storage topology.** Same-node replicas share the persistent working set. Fixed multi-node deployments use explicit storage-node identities, node routing, and S3 file storage, preserving user-and-Agent storage affinity.

Horizontal scaling expands concurrent execution capacity; it does not automatically accelerate a single task or transparently migrate running tasks across machines. Model quotas, databases, queues, and storage still affect capacity. See [multiple Workers and machines](deployment.md) for deployment requirements.

## Domain objects

A Conversation is the user dialogue; a Task is the product execution record; a Reply is an AgentScope reply execution. `tasks.run_id` stores `reply_id`.

RuntimeSession identifies the runtime conversation, and Workspace stores execution files. User-visible message snapshots, Core state snapshots, and Workspace file checkpoints are distinct evidence and cannot substitute for each other.

Organization, Department, and Personal Scopes define resource boundaries. The product authorizes access before assembling the exact capabilities into execution. Shared service credentials do not change caller identity.

## From authorization to execution

![RBAC + Scope: separate management, use, binding, and external identity checks](../assets/diagrams/access-governance-en.svg)

Role permissions and Scope boundaries constrain each action, while resource sharing requires explicit grants. See [administration and security](administration.md) for the complete policy.

## Consistency

PostgreSQL stores execution owners, epochs, leases, and business facts. Redis Streams transports messages; duplicate delivery cannot independently grant execution authority. Start, Resume, and Stop are associated with transactional outbox intents.

An expired lease alone does not prove that an old process, tool, or workspace has stopped. Recovery must check ownership and durable resource evidence, retaining isolation when safe cleanup cannot be proven. Message deduplication does not give external side effects exactly-once semantics.

Applications manage transactions. Repositories encapsulate SQL and locks within the caller's transaction. Network calls stay outside long transactions and lock-holding sections to limit contention.

## Events and artifacts

`runtime_seq`, `product_seq`, `business_seq`, and Redis transport cursors have distinct roles. Bounded Streams carry realtime body text; the database retains business facts and message snapshots. Browsers outside the replay window perform an explicit reset rather than treating truncated data as complete history.

`task_events` stores product audit events. Tool Activity, browser replay, and runtime message transport maintain separate records for their respective purposes. Artifacts are projected from `present_files` declarations or file differences, with product-authorized downloads.

## AgentScope contracts

Core events identify execution through `session_id` / `reply_id`. A synthetic ReplyStart during approval resume can reuse the same `reply_id`; consumers must not clear accumulated body text.

Workspace Skill/MCP operations are scoped by Agent and Session. ReMe runs inside Runtime. Worker calls Core directly; the product API exposes the external HTTP and event-stream interfaces.

See [tasks and recovery](runtime.md) for execution details and [limitations](limitations.md) for deployment boundaries.
