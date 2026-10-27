"""Workspace ownership ports; no filesystem or distributed-lock implementation.

Providers must establish exclusive access before returning a lease, retain a
crash recovery barrier, and reject unproved handoffs. A TTL lock by itself does
not satisfy this contract. Only trusted composition supplies implementations.
"""
from pathlib import Path
from typing import Protocol


class WorkspaceOwnershipError(RuntimeError):
    """Exclusive Workspace access or its durable identity cannot be established."""


class WorkspaceOwnershipLease(Protocol):
    @property
    def path(self) -> Path: ...

    @property
    def generation(self) -> str: ...

    def close(self, *, clean: bool) -> None:
        """Release; clean requires drained resources, failure must retain the barrier.

        clean=False must preserve the crash latch even after exclusion is released.
        Retrying a completed close is harmless; a failed clean close is retryable.
        """
        ...


class WorkspaceOwnershipProvider(Protocol):
    def acquire(self, basedir: Path, workspace_id: str,
                identity: dict[str, str]) -> WorkspaceOwnershipLease:
        """Bind exact identity and exclude peers before any Workspace initialization.

        Existing unclean or unverifiable state must block. Returned path belongs
        to this identity; generation identifies storage lifetime, not owner epoch.
        Acquisition failure must not authorize starting any resource.
        """
        ...
