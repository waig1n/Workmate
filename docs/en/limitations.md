# Support boundaries and limitations

[English](../en/limitations.md) | [简体中文](../zh-CN/limitations.md)

Use the following support matrix to plan your environment, integrations, and recovery requirements.

| Area | Support and requirements |
|---|---|
| Host environment | The default deployment uses Linux and Bubblewrap, with host namespace support and the required container permissions |
| Sandboxes | Isolation, storage, suspension, and recovery vary by backend; Local provides no sandbox isolation |
| Horizontal scaling | Multiple Worker replicas and fixed multi-node deployments are supported; each conversation remains serial, and scaling requires storage affinity and node routing |
| Failure recovery | Eligible failures support explicit recovery and optional file checkpoints; running tasks are not transparently taken over across hosts |
| Data recovery | Database records, Core state, working files, and long-term memory persist separately; file checkpoints may lag behind recent changes |
| Capacity and reliability | Concurrency depends on model quotas, databases, queues, storage, and sandbox resources; test with your expected workload |
| Model media | Input formats require support from both the adapter and provider endpoint; verify configured formats with actual requests |
| Knowledge retrieval | Documents are searchable after indexing; assess retrieval quality using representative documents and questions |
| Enterprise messaging | Configure credentials, identity bindings, and permissions for each platform; personal WeChat is experimental and proactive messages depend on recent conversation context |
| Unattended execution | Actions requiring human approval are rejected; tasks do not wait for confirmation |
| Cost reporting | Usage and cost estimates support analysis; provider invoices determine charges. Billing settlement and hard budget enforcement are not provided |
| External actions | Messaging and business writes require integration-specific idempotency; unknown outcomes are not replayed automatically |
| Historical resource cleanup | Cross-node cleanup of old workspaces has additional requirements; resources are retained when safe cleanup cannot be established |

Workmate is under active development. Before production deployment, verify permissions, approvals, Stop, file delivery, and backup restoration in your target environment. Test the models and external integrations you will use. See [deployment and operations](deployment.md) for setup and [testing and operational tools](tooling.md) for load tests and fault drills.
