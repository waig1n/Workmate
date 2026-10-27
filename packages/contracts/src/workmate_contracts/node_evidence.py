"""Node observations for permission-parked recovery, never execution authority.

These are strict, engine-free wire values. Successful validation only establishes
shape and internal consistency. A Product service must authenticate the node
observer, verify its measurements independently and compare every identity,
watermark, digest and observation with the durable ledger being atomically frozen.
"""
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator
from workmate_contracts import Contract, MAX_PAYLOAD_BYTES, snapshot_digest

EvidenceIdentity = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^\S+$')]
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Sequence = Annotated[int, Field(ge=1, le=9_223_372_036_854_775_807)]
Epoch = Annotated[int, Field(ge=1)]


class NodeEvidenceContract(Contract):
    def digest(self) -> str:
        return snapshot_digest(self.model_dump(mode='json'))


class NodeContainment(NodeEvidenceContract):
    """A host-observed runtime incarnation, not a container display name.

    generation is allocated by the trusted authority and must never be reused.
    PID namespace identity is measured from the host, not a replacement
    container's /proc. A cgroup path alone is not a stable incarnation identity.
    """
    kind: Literal['container','cgroup']
    runtime_instance_id: EvidenceIdentity
    generation: EvidenceIdentity
    pid_namespace_inode: Annotated[int, Field(ge=1)]
    cgroup_path: Annotated[str, Field(min_length=1, max_length=4096)] | None = None

    @model_validator(mode='after')
    def validate_cgroup_path(self):
        path=self.cgroup_path
        if path is not None and (not path.startswith('/') or '\\' in path
                or any(part in {'.','..'} for part in path.split('/'))
                or any(ord(char)<32 or ord(char)==127 for char in path)):
            raise ValueError('Cgroup identity requires an absolute host-observed path')
        if self.kind=='cgroup' and path is None:
            raise ValueError('Cgroup containment requires its host-observed path')
        return self


class WorkerIncarnation(NodeEvidenceContract):
    """worker_id is unique to one process incarnation, never a stable node name."""
    worker_id: EvidenceIdentity
    storage_node_id: EvidenceIdentity
    node_authority_id: EvidenceIdentity
    host_boot_id: Annotated[str, Field(pattern=r'^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')]
    host_pid: Annotated[int, Field(ge=1)]
    process_start_ticks: Annotated[int, Field(ge=0)]
    containment: NodeContainment


class WorkspaceGeneration(NodeEvidenceContract):
    session_id: EvidenceIdentity
    conversation_id: EvidenceIdentity
    storage_node_id: EvidenceIdentity
    generation: EvidenceIdentity
    volume_id: EvidenceIdentity
    workspace_id: EvidenceIdentity


class ResourceRegistration(NodeEvidenceContract):
    """An append-only ledger fact; unknown reservations cannot disappear.

    A reserved/unknown launch cannot form a recoverable frozen ledger. These
    records do not themselves resolve a reservation into a successful launch.
    A separate immutable ResourceLaunchResolution binds a trusted launch fence
    and outcome; actual Worker process creation still requires node gate wiring.
    """
    resource_id: EvidenceIdentity
    worker_id: EvidenceIdentity
    session_id: EvidenceIdentity
    task_id: EvidenceIdentity
    execution_epoch: Epoch
    registration_seq: Sequence
    kind: Literal['process','session_infrastructure','external_job']
    launch_state: Literal['reserved','started','unknown']
    original_tool_call_id: EvidenceIdentity | None = None
    containment_ref: EvidenceIdentity | None = None

    @model_validator(mode='after')
    def validate_resource_scope(self):
        if self.kind=='external_job' and self.original_tool_call_id is None:
            raise ValueError('External job requires its original ToolCall identity')
        if self.launch_state=='started' and self.kind!='external_job' and self.containment_ref is None:
            raise ValueError('Started local resource requires trusted containment identity')
        return self


class PermissionParkedBoundary(NodeEvidenceContract):
    """Only an exact permission boundary; submitted external calls are excluded."""
    kind: Literal['permission'] = 'permission'
    status: Literal['awaiting_interaction'] = 'awaiting_interaction'
    snapshot_id: EvidenceIdentity
    snapshot_sha256: Digest
    state_version: Sequence
    runtime_seq: Sequence
    reply_id: EvidenceIdentity
    request_version: EvidenceIdentity
    pending_tool_call_ids: Annotated[tuple[EvidenceIdentity,...], Field(min_length=1, max_length=4096)]

    @model_validator(mode='after')
    def unique_pending_calls(self):
        if len(set(self.pending_tool_call_ids))!=len(self.pending_tool_call_ids):
            raise ValueError('Pending original ToolCall identities must be unique')
        return self


class ResourceLaunchResolution(NodeEvidenceContract):
    """Immutable measured outcome for a local reservation, not launch authority.

    launch_fence_ref identifies an independently verified irrevocable node gate
    that prevents the original launch from running later. A string supplied by
    a caller is not proof that such a gate exists. Product intake must verify
    and persist the measurement before using it; actual launch wiring is separate.
    """
    registration: ResourceRegistration
    authority_id: EvidenceIdentity
    outcome: Literal['started','not_started']
    containment_ref: EvidenceIdentity | None = None
    observation_ref: EvidenceIdentity
    observed_at: AwareDatetime
    launch_fence_ref: EvidenceIdentity

    @model_validator(mode='after')
    def validate_local_resolution(self):
        if self.registration.kind=='external_job' or self.registration.launch_state not in {'reserved','unknown'}:
            raise ValueError('Only unresolved local launch registrations can be resolved')
        if (self.outcome=='started') != (self.containment_ref is not None):
            raise ValueError('Only a started launch resolution carries containment identity')
        return self


class ResourceObservation(NodeEvidenceContract):
    registration: ResourceRegistration
    state: Literal['terminated','completed','active','unknown','not_started']
    # Reference to a trusted measurement, not unstructured exception/log text.
    observation_ref: EvidenceIdentity | None = None
    resolution: ResourceLaunchResolution | None = None

    @model_validator(mode='after')
    def consistent_resolution(self):
        if self.resolution is not None:
            if self.resolution.registration!=self.registration:
                raise ValueError('Launch resolution must preserve the exact original registration')
            if (self.resolution.outcome=='not_started') != (self.state=='not_started'):
                raise ValueError('Never-started observations require matching launch resolution')
        elif self.state=='not_started':
            raise ValueError('Never-started observation requires an irrevocable launch resolution')
        return self


class NodeQuiescenceEvidence(NodeEvidenceContract):
    """Claimed measurements, requiring authenticated independent verification.

    confirmed requests atomic Product ledger freezing; the sender cannot declare
    a database ledger already closed. It is not an ownership-transfer grant. No accepted evidence can
    authorize automatic Worker recovery or bypass current Product permissions.
    The scope deliberately excludes all external-job registrations, even if a
    sender claims completion; remote-job recovery needs a separate protocol.
    """
    evidence_id: EvidenceIdentity
    authority_id: EvidenceIdentity
    scope: Literal['permission_parked_no_external_jobs'] = 'permission_parked_no_external_jobs'
    worker: WorkerIncarnation
    workspace: WorkspaceGeneration
    task_id: EvidenceIdentity
    execution_epoch: Epoch
    boundary: PermissionParkedBoundary
    registered_through_seq: Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]
    ledger_sha256: Digest
    observed_at: AwareDatetime
    worker_state: Literal['terminated','active','unknown']
    containment_state: Literal['terminated','active','unknown']
    conclusion: Literal['confirmed','unknown']
    observations: Annotated[tuple[ResourceObservation,...], Field(max_length=4096)]

    @model_validator(mode='after')
    def consistent_observations(self):
        if self.authority_id!=self.worker.node_authority_id:
            raise ValueError('Evidence issuer differs from the registered node authority')
        if self.workspace.storage_node_id!=self.worker.storage_node_id:
            raise ValueError('Worker and Workspace belong to different storage nodes')
        ids=set()
        sequences=[]
        for observation in self.observations:
            resource=observation.registration
            if (resource.worker_id!=self.worker.worker_id or resource.session_id!=self.workspace.session_id
                    or resource.task_id!=self.task_id or resource.execution_epoch!=self.execution_epoch):
                raise ValueError('Resource observation belongs to another execution')
            if resource.resource_id in ids:
                raise ValueError('Resource observations cannot be duplicated')
            if observation.resolution is not None and (observation.resolution.authority_id!=self.authority_id
                    or observation.resolution.observed_at>self.observed_at):
                raise ValueError('Launch resolution belongs to another authority or a later observation')
            ids.add(resource.resource_id)
            sequences.append(resource.registration_seq)
        if len(set(sequences))!=len(sequences) or any(seq>self.registered_through_seq for seq in sequences):
            raise ValueError('Resource observation sequence is duplicated or exceeds its watermark')
        if self.conclusion=='confirmed':
            if (self.worker_state!='terminated' or self.containment_state!='terminated'
                    or self.registered_through_seq<1 or len(sequences)!=self.registered_through_seq
                    or any(sequence!=expected for expected,sequence in enumerate(sorted(sequences),1))):
                raise ValueError('Confirmed quiescence requires complete observations and a terminated incarnation')
            started_root=False
            for observation in self.observations:
                resource=observation.registration
                resolution=observation.resolution
                started=resource.launch_state=='started' or (resolution is not None and resolution.outcome=='started')
                containment=resource.containment_ref if resolution is None else resolution.containment_ref
                never_started=resolution is not None and resolution.outcome=='not_started' and observation.state=='not_started'
                if (resource.kind=='external_job' or observation.observation_ref is None
                        or not (never_started or (started and observation.state=='terminated'
                            and containment==self.worker.containment.runtime_instance_id))):
                    raise ValueError('Unresolved launches, remote jobs or unproved resource exits cannot confirm quiescence')
                started_root=started_root or (resource.kind=='session_infrastructure' and started)
            if not started_root:
                raise ValueError('Confirmed quiescence requires a started root Session infrastructure')
        if len(self.model_dump_json().encode('utf-8'))>MAX_PAYLOAD_BYTES:
            raise ValueError('Node evidence exceeds the contract payload bound')
        return self
