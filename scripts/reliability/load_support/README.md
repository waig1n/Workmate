# Reliability workload tools

[English guide](../../../docs/en/tooling.md) | [中文指南](../../../docs/zh-CN/tooling.md)

The deterministic workload covers task admission, Worker execution, approvals, SSE, and artifact delivery in an isolated environment. Controlled model and dependency fixtures make runs repeatable. Provider quality, external MCP services, persistent knowledge infrastructure, and production capacity require separate integration tests.

```bash
python3 -m scripts.reliability.load --help
```

Review image, resource, and target arguments before running a workload. Commands preview the plan by default. Cross-host runs require an explicit target and the corresponding infrastructure. Keep reports and credentials outside public source control.
