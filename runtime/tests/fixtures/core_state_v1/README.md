# AgentScope Core state fixtures

Fixed AgentScope 2.0.7 snapshots generated with deterministic model and tool fixtures. Tests load these samples without regenerating them. They contain no provider credentials or user data.

| File | State |
|---|---|
| `normal.json` | Completed reply; context is retained on the next turn |
| `parked.json` | `call-1` awaits approval and has not executed |
| `partial.json` | `call-1` completed; `call-2` still awaits approval |

`format_version=1` and `engine_version=2.0.7` identify the supported format and engine. Keep these snapshots when adding migration coverage or new formats so tests continue to check compatibility with existing state. Full execution recovery also requires trusted configuration, tools, and workspace state.

See [tasks and recovery](../../../../docs/en/runtime.md) or the [Chinese guide](../../../../docs/zh-CN/runtime.md).
