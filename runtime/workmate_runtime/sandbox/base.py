"""Server-owned sandbox contracts; ownership and fencing remain with the Worker."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class WorkspaceBindingError(RuntimeError):
    """A workspace or Task binding does not match the Worker's trusted scope."""


@dataclass(frozen=True)
class SandboxWorkspaceRequest:
    """Logical identity plus optional local storage; remote providers need no mount."""
    workspace_id: str
    local_workdir: Path | None = None
    expected_storage_proof: dict | None = None
    placement_path: Path | None = None


class SandboxProvider(Protocol):
    name: str

    def create_workspace(self, request: SandboxWorkspaceRequest):
        """Construct synchronously so the manager retains partial initialization."""
        ...

    async def initialize_workspace(self, workspace) -> None: ...
    async def close_workspace(self, workspace) -> None: ...
    def bind_backend(self, workspace, owner, registry): ...
    async def verify_task_isolation(self, backend) -> dict | None: ...
    async def probe(self) -> None: ...


class TrustedSandboxControlBackend:
    """Audited provider management channel, separate from sealed Task scopes."""
