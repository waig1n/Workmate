"""Product workspace helpers, sandbox backends and tools using AgentScope Core."""

import asyncio
import base64
import fnmatch
import hashlib
import logging
import os
import posixpath
import subprocess
from typing import Any

from agentscope.message import Base64Source, DataBlock, TextBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import Bash, Edit, Read, ToolChunk, Write
from agentscope.workspace import BubblewrapBackend
from agentscope.workspace import BubblewrapWorkspace

logger = logging.getLogger(__name__)

# Product-layer workspace convention (design §17).
CONVENTION_DIRS = ("input", "working", "output")

_NATIVE_MEDIA_EXTENSIONS = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".mov": "video/quicktime",
    ".avi": "video/avi",
    ".webm": "video/webm",
}

_NESTED_BWRAP_PROBE = (
    "bwrap",
    "--die-with-parent",
    "--new-session",
    "--unshare-pid",
    "--unshare-ipc",
    "--unshare-uts",
    "--unshare-cgroup-try",
    "--ro-bind",
    "/",
    "/",
    "--proc",
    "/proc",
    "--dev",
    "/dev",
    "--cap-drop",
    "ALL",
    "--",
    "true",
)


def _stable_id(key: str) -> str:
    """Short deterministic id (mirrors the stock PER_AGENT hashing)."""
    return hashlib.blake2b(key.encode(), digest_size=8).hexdigest()


def ensure_convention_dirs(workdir: str) -> None:
    """Create convention directories on a local filesystem."""
    for sub in CONVENTION_DIRS:
        os.makedirs(os.path.join(workdir, sub), exist_ok=True)


async def ensure_workspace_convention(workspace) -> None:
    """Create product directories through any AgentScope backend."""
    backend = workspace.get_backend()
    paths = [
        backend.join_path(workspace.workdir, sub)
        for sub in CONVENTION_DIRS
    ]
    await backend.exec_shell(["mkdir", "-p", *paths])


def _sandbox_file_to_host(
    file_path: str,
    *,
    sandbox_workdir: str,
    host_workdir: str | None,
) -> str | None:
    """Resolve a sandbox workspace path to its mounted host path.

    AgentScope 2.0.7's ``ToolContext`` validates Read/Write/Edit caches with
    host ``os.path.getmtime`` calls. Bubblewrap tools, however, expose paths
    below ``/workspace`` to the Agent. Only translate paths inside that
    mount, and reject symlink/traversal escapes before the host stat occurs.
    """
    sandbox_root = posixpath.normpath(sandbox_workdir)
    normalized = posixpath.normpath(file_path)
    prefix = sandbox_root.rstrip("/") + "/"
    if normalized == sandbox_root or not normalized.startswith(prefix):
        return None

    if host_workdir is None:
        return normalized

    relative = posixpath.relpath(normalized, sandbox_root)
    host_root = os.path.realpath(host_workdir)
    candidate = os.path.realpath(
        os.path.join(host_root, *relative.split("/")),
    )
    try:
        if os.path.commonpath([host_root, candidate]) != host_root:
            return None
    except ValueError:
        return None
    return candidate


class _WorkmateSandboxCachePaths:
    """Map Agent-visible paths to host-backed read evidence."""

    def _configure_cache_paths(
        self,
        *,
        sandbox_workdir: str,
        host_workdir: str | None,
    ) -> None:
        self._workmate_sandbox_workdir = sandbox_workdir
        self._workmate_host_workdir = host_workdir

    def _cache_path(self, file_path: str) -> str | None:
        return _sandbox_file_to_host(
            file_path,
            sandbox_workdir=self._workmate_sandbox_workdir,
            host_workdir=self._workmate_host_workdir,
        )

    @staticmethod
    def _remember_read(
        agent_state: AgentState,
        cache_path: str,
        raw: bytes,
    ) -> None:
        evidence = agent_state.middle_context.setdefault(
            "workmate_sandbox_read_evidence",
            {},
        )
        evidence.pop(cache_path, None)
        evidence[cache_path] = hashlib.sha256(raw).hexdigest()
        while len(evidence) > 100:
            evidence.pop(next(iter(evidence)))

    async def _has_current_read(
        self,
        agent_state: AgentState,
        cache_path: str | None,
    ) -> bool:
        if cache_path is None:
            return False
        evidence = agent_state.middle_context.get(
            "workmate_sandbox_read_evidence",
            {},
        )
        expected = evidence.get(cache_path)
        if expected is None:
            return False
        try:
            if self._workmate_host_workdir is None:
                current = hashlib.sha256(await self._backend.read_file(cache_path)).hexdigest()
            else:
                with open(cache_path, "rb") as file:
                    current = hashlib.sha256(file.read()).hexdigest()
        except (OSError, ValueError):
            evidence.pop(cache_path, None)
            return False
        if current != expected:
            evidence.pop(cache_path, None)
            return False
        return True


def _file_tool_error(message: str) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=message)],
        state=ToolResultState.ERROR,
        is_last=True,
    )


class WorkmateSandboxRead(_WorkmateSandboxCachePaths, Read):
    """Read that records cache evidence against the mounted host file.

    ``model_input_types`` is bound from the chat model profile's
    capability declaration so a text-only model gets an explicit
    "Unsupported image type" error instead of a base64 DataBlock that the
    provider would reject the whole reply for.
    """

    def __init__(
        self,
        *,
        backend,
        sandbox_workdir: str,
        host_workdir: str | None,
        model_input_types: list[str] | None = None,
    ):
        # None means "capability undeclared" — fail closed to text-only
        # rather than letting the upstream default (image types included)
        # reintroduce the provider 400 this class exists to prevent.
        super().__init__(
            backend=backend,
            model_input_types=list(model_input_types or ["text/plain"]),
        )
        self._configure_cache_paths(
            sandbox_workdir=sandbox_workdir,
            host_workdir=host_workdir,
        )

    async def _read_text_file(
        self,
        file_path: str,
        offset: int,
        limit: int,
        _agent_state: AgentState | None,
    ):
        media_type = _NATIVE_MEDIA_EXTENSIONS.get(
            os.path.splitext(file_path)[1].lower(),
        )
        if media_type is not None:
            return await self._read_native_media(
                file_path,
                media_type,
                _agent_state,
            )
        result = await super()._read_text_file(
            file_path,
            offset,
            limit,
            # Upstream ToolContext stats ``file_path`` directly. Passing the
            # Agent-visible path here would repeat the Bubblewrap bug before
            # we can install the mapped host cache below.
            None,
        )
        if _agent_state is None or result.state == ToolResultState.ERROR:
            return result

        cache_path = self._cache_path(file_path)
        if cache_path is None:
            return result
        try:
            raw = await self._backend.read_file(file_path)
            self._remember_read(_agent_state, cache_path, raw)
        except Exception:
            # The Read result remains authoritative. If the second read
            # races with another writer, the later Write/Edit must ask for a
            # fresh Read instead of trusting stale evidence.
            pass
        return result

    async def _read_native_media(
        self,
        file_path: str,
        media_type: str,
        agent_state: AgentState | None,
    ) -> ToolChunk:
        """Return configured audio/video as a DataBlock.

        AgentScope 2.0.7's builtin Read dispatches only images and PDFs;
        all other extensions fall through to UTF-8 text decoding. workmate
        closes that gap for media types explicitly allowed by the selected
        model profile. The provider Formatter remains responsible for the
        wire representation (for example DashScope ``video_url``).
        """
        if not any(
            fnmatch.fnmatch(media_type, pattern)
            for pattern in self.model_input_types
        ):
            return _file_tool_error(
                f"Error: Unsupported media type {media_type} for the "
                "selected model profile.",
            )
        try:
            raw = await self._backend.read_file(file_path)
        except Exception as exc:  # noqa: BLE001 - tool error boundary
            return _file_tool_error(f"Error reading file: {exc}")

        if agent_state is not None:
            cache_path = self._cache_path(file_path)
            if cache_path is not None:
                self._remember_read(agent_state, cache_path, raw)
        return ToolChunk(
            content=[
                DataBlock(
                    source=Base64Source(
                        data=base64.b64encode(raw).decode("ascii"),
                        media_type=media_type,
                    ),
                    name=self._backend.basename(file_path),
                ),
            ],
            state=ToolResultState.RUNNING,
            is_last=True,
        )


class WorkmateSandboxWrite(_WorkmateSandboxCachePaths, Write):
    """Write that preserves read-before-overwrite inside Bubblewrap."""

    def __init__(self, *, backend, sandbox_workdir: str, host_workdir: str | None):
        super().__init__(backend=backend)
        self._configure_cache_paths(
            sandbox_workdir=sandbox_workdir,
            host_workdir=host_workdir,
        )

    async def call(
        self,
        file_path: str,
        content: str,
        _agent_state: AgentState | None = None,
    ):
        if _agent_state is not None and await self._backend.file_exists(
            file_path,
        ):
            cache_path = self._cache_path(file_path)
            if await self._has_current_read(_agent_state, cache_path):
                # The mapped cache has already enforced read-before-write.
                # Let the upstream implementation perform the actual backend
                # write without repeating its invalid virtual-path stat.
                return await super().call(file_path, content, None)
            return _file_tool_error(
                f"Error: File {file_path} exists but has not been read yet. "
                "You must read the file first before writing to it.",
            )
        return await super().call(file_path, content, None)


class WorkmateSandboxEdit(_WorkmateSandboxCachePaths, Edit):
    """Edit that preserves read-before-edit inside Bubblewrap."""

    def __init__(self, *, backend, sandbox_workdir: str, host_workdir: str | None):
        super().__init__(backend=backend)
        self._configure_cache_paths(
            sandbox_workdir=sandbox_workdir,
            host_workdir=host_workdir,
        )

    async def call(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        _agent_state: AgentState | None = None,
    ):
        if _agent_state is not None:
            cache_path = self._cache_path(file_path)
            if await self._has_current_read(_agent_state, cache_path):
                return await super().call(
                    file_path,
                    old_string,
                    new_string,
                    replace_all,
                    None,
                )
            return _file_tool_error(
                "Error: To edit a file, you must first read it using the "
                "Read tool.",
            )
        return await super().call(
            file_path,
            old_string,
            new_string,
            replace_all,
            None,
        )


class WorkmateSandboxBash(Bash):
    """Keep shell execution attached to the Product Task lifecycle.

    AgentScope's framework ``ToolOffloadMiddleware`` turns any ordinary tool
    that runs for more than ten seconds into a background task.  Its result is
    delivered later through an idle-session wake-up with a new ``reply_id``.
    workmate intentionally maps one Product Task to one AgentScope Reply and
    does not yet project autonomous wake-up replies as continuations.  Letting
    Bash offload would therefore allow the original Reply to report COMPLETED
    before a requested file has been generated.

    State-injected tools are deliberately excluded from upstream offloading.
    Bash does not use AgentState, but accepting the injected argument gives us
    that lifecycle property while retaining Bash's own timeout (up to ten
    minutes), sandbox and permission checks.
    """

    is_state_injected: bool = True

    async def call(
        self,
        command: str,
        description: str = "",
        timeout: int = 120000,
        _agent_state: AgentState | None = None,
    ):
        del _agent_state
        async for chunk in super().call(command, description, timeout):
            yield chunk


class WorkmateClearedEnvBubblewrapBackend(BubblewrapBackend):
    """Bubblewrap backend whose PID-namespace init has no Runtime secrets."""

    @staticmethod
    def _launcher_env() -> dict[str, str]:
        return {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
        }

    async def start_process(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        stdin: Any = asyncio.subprocess.DEVNULL,
        stdout: Any = asyncio.subprocess.DEVNULL,
        stderr: Any = asyncio.subprocess.DEVNULL,
    ) -> asyncio.subprocess.Process:
        argv = self._bwrap_argv(command, cwd=cwd)
        kwargs: dict[str, Any] = {"env": self._launcher_env()}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            **kwargs,
        )


class WorkmateNestedBubblewrapBackend(WorkmateClearedEnvBubblewrapBackend):
    """Bubblewrap argv compatible with Docker's outer user namespace.

    The Runtime container supplies ``CAP_SYS_ADMIN`` solely so Bubblewrap can
    create mount/PID namespaces. Every Agent command runs after
    ``--cap-drop ALL`` and cannot retain that container capability.
    """

    def _bwrap_argv(
        self,
        command: list[str],
        *,
        cwd: str | None,
    ) -> list[str]:
        args = super()._bwrap_argv(command, cwd=cwd)
        unshare_index = args.index("--unshare-all")
        args[unshare_index : unshare_index + 1] = [
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
        ]
        # No network namespace is created in the nested-container variant.
        # The gateway still needs loopback connectivity across executions.
        if "--share-net" in args:
            args.remove("--share-net")
        command_index = args.index("--")
        args[command_index:command_index] = ["--cap-drop", "ALL"]
        return args


class WorkmateSandboxWorkspace(BubblewrapWorkspace):
    """Bubblewrap workspace whose governed MCP config is memory-only.

    AgentScope's default persistent workspace writes MCP declarations,
    including headers/environment values, to ``/workspace/.mcp``. That path
    is intentionally Agent-readable, so governed credentials must never be
    persisted there. Product preflight repopulates the in-memory gateway from
    the Runtime library before every run.
    """

    def __init__(
        self,
        *,
        read_model_input_types: list[str] | None = None,
        **kwargs,
    ) -> None:
        # Text-only default: an undeclared model must not receive DataBlocks
        # it cannot consume. Multimodal deployments declare the capability
        # explicitly on the model profile. Worker assembly binds the selected
        # Task model capabilities directly.
        self._read_model_input_types = list(read_model_input_types or [
            "text/plain",
        ])
        super().__init__(**kwargs)


    async def _save_mcp_file(self) -> None:
        """Keep secret-bearing MCP declarations out of Agent-visible files."""

    async def _restore_mcp_specs(self) -> dict[tuple[str, str], list]:
        """MCP declarations are restored only by governed pre-run sync."""
        return {}

    async def list_tools(self) -> list:
        """Bind file tools with Bubblewrap-aware read-cache validation."""
        from agentscope.tool import Glob, Grep

        backend = self.get_backend()
        glob_kwargs: dict = {"backend": backend}
        if self._glob_helper_path is not None:
            glob_kwargs["glob_helper_path"] = self._glob_helper_path
        cache_kwargs = {
            "backend": backend,
            "sandbox_workdir": self.workdir,
            "host_workdir": self.host_workdir,
        }
        return [
            WorkmateSandboxBash(cwd=self.workdir, backend=backend),
            WorkmateSandboxEdit(**cache_kwargs),
            Glob(**glob_kwargs),
            Grep(backend=backend),
            WorkmateSandboxRead(
                **cache_kwargs,
                model_input_types=self._read_model_input_types,
            ),
            WorkmateSandboxWrite(**cache_kwargs),
        ]

    @staticmethod
    async def _probe_bubblewrap() -> None:
        """Use a Docker-compatible probe while retaining host defaults."""
        if not os.path.exists("/.dockerenv"):
            await BubblewrapWorkspace._probe_bubblewrap()
            return
        completed = await asyncio.to_thread(
            subprocess.run,
            _NESTED_BWRAP_PROBE,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).decode(
                "utf-8",
                errors="replace",
            )
            raise RuntimeError(
                "Bubblewrap nested-container smoke probe failed: " + detail,
            )

    async def _provision_backend(self) -> None:
        await super()._provision_backend()
        assert self._tmpdir is not None
        backend_class = (
            WorkmateNestedBubblewrapBackend
            if os.path.exists("/.dockerenv")
            else WorkmateClearedEnvBubblewrapBackend
        )
        self._backend = backend_class(
            host_workdir=self.host_workdir,
            host_tmpdir=self._tmpdir.name,
            host_cache_dir=self._host_cache_dir or None,
            workdir=self.workdir,
            share_net=self.share_net,
            env=self.env,
        )

    @property
    def _gateway_python(self) -> str:
        # The Runtime image already contains AgentScope and gateway
        # dependencies under /usr/local. Reusing that read-only installation
        # avoids downloading a private venv for every conversation.
        if os.path.exists("/usr/local/bin/python"):
            return "/usr/local/bin/python"
        return super()._gateway_python

    async def _bootstrap_is_ready(self) -> bool:
        if self._gateway_python == "/usr/local/bin/python":
            # This interpreter has no bootstrap/install commands. Starting the
            # actual gateway and verifying its nonce-bound health is the readiness
            # check; a second import-only process adds no recovery or safety.
            return True
        return await super()._bootstrap_is_ready()

    def _bootstrap_commands(self) -> list[str]:
        if self._gateway_python == "/usr/local/bin/python":
            return []
        return super()._bootstrap_commands()

