# -*- coding: utf-8 -*-
"""ORM models - the product data model (design §21).

``task_events`` is the single Product task audit table; Tool Audit is a
filtered view over it. Runtime outbox rows are transport records, not audit.
"""

import enum
from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    DDL,
    event,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_email_default(context) -> str:
    """Derive normalized_email from the email in the same insert.

    Keeps ORM/Core inserts (tests, scripts, admin user creation) from
    having to set it explicitly; the auth router still passes it
    explicitly for clarity (user management plan §3.2).
    """
    email = context.current_parameters.get("email")
    return (email or "").strip().casefold()


class Base(DeclarativeBase):
    pass


class TaskStatus(str, enum.Enum):
    CREATED = "created"
    RUNNING = "running"
    AWAITING_PERMISSION = "awaiting_permission"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ApprovalProfile(str, enum.Enum):
    """Stable product-level policy selected for one Task."""

    STANDARD = "standard"
    STRICT = "strict"
    READ_ONLY = "read_only"
    FULL_ACCESS = "full_access"
    UNATTENDED = "unattended"


ACTIVE_TASK_STATUSES = (
    TaskStatus.CREATED,
    TaskStatus.RUNNING,
    TaskStatus.AWAITING_PERMISSION,
    TaskStatus.CANCELLING,
)


class ScopeKind(str, enum.Enum):
    PERSONAL = "personal"
    ORGANIZATION = "organization"
    DEPARTMENT = "department"


class Visibility(str, enum.Enum):
    PRIVATE = "private"
    PUBLIC = "public"


class CapabilityType(str, enum.Enum):
    SKILL = "skill"
    MCP = "mcp"


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Display value; uniqueness and login lookups use normalized_email
    # (user management plan §3.2).
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    normalized_email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True,
        default=_normalized_email_default,
    )
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(100))
    access_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    profile_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # Persist the public protocol value directly.  Do not use SQLAlchemy
    # Enum here: even native_enum=False serializes enum member names
    # (ACTIVE/DISABLED), coupling storage to Python implementation names.
    status: Mapped[str] = mapped_column(
        String(16),
        default=UserStatus.ACTIVE.value,
        server_default=UserStatus.ACTIVE.value,
    )
    # Bumped on disable and password changes to revoke issued JWTs.
    token_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0",
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0",
    )
    # Cached runtime-side credential id for the chat model API key.
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    # Client-side default keeps microsecond precision, which the
    # (created_at, id) keyset cursor in the admin user list relies on
    # for equality round-trips; server_default remains the fallback.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(),
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    disabled_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"),
    )


class Scope(Base):
    """Resource ownership scope (RBAC design §4.1).

    ``personal`` scopes have no parent and exactly one owner user;
    ``organization`` is the tree root; ``department`` nodes hang below
    an organization or department. Constraints are enforced by the
    service layer and the bootstrap/migration paths.
    """

    __tablename__ = "scopes"
    tree_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    __table_args__ = (
        # Sibling departments are unique per parent after name casefolding.
        # The partial predicate uses lowercase kind values.
        Index(
            "uq_scopes_department_sibling_name",
            "parent_id", "normalized_name", unique=True,
            postgresql_where=text("kind = 'department'"),
            sqlite_where=text("kind = 'department'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("scopes.id"), index=True,
    )
    # Set (unique) only for personal scopes.
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), unique=True,
    )
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(
        String(200), default=lambda context: (
            context.get_current_parameters().get("name") or ""
        ).strip().casefold(),
        server_default="",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(),
    )


class ScopeClosure(Base):
    """Ancestor/descendant index over the scope tree (RBAC design §4.1).

    Every scope carries ``(self, self, 0)``.
    """

    __tablename__ = "scope_closure"

    ancestor_id: Mapped[str] = mapped_column(
        ForeignKey("scopes.id"), primary_key=True,
    )
    descendant_id: Mapped[str] = mapped_column(
        ForeignKey("scopes.id"), primary_key=True,
    )
    depth: Mapped[int] = mapped_column(Integer)


class ScopeMembership(Base):
    """User belongs to a scope; roles live in RoleBinding (RBAC §4.2)."""

    __tablename__ = "scope_memberships"

    scope_id: Mapped[str] = mapped_column(
        ForeignKey("scopes.id"), primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), primary_key=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    delegatable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    display_name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(
        String(500), default="", server_default="",
    )
    # System roles (admin/member) are seeded by code; their keys and
    # grants are not editable in the management UI (plan §3.4).
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(),
        onupdate=func.now(),
    )


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(
        String(100), default="", server_default="",
    )
    description: Mapped[str] = mapped_column(
        String(500), default="", server_default="",
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(),
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"), index=True)
    permission_id: Mapped[str] = mapped_column(
        ForeignKey("permissions.id"), index=True,
    )


class RoleBinding(Base):
    """User holds a role in a scope (RBAC design §5).

    ``include_descendants`` controls whether the binding applies to
    descendant scopes; it is independent of resource-level inheritance.
    """

    __tablename__ = "role_bindings"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), primary_key=True,
    )
    role_id: Mapped[str] = mapped_column(
        ForeignKey("roles.id"), primary_key=True,
    )
    scope_id: Mapped[str] = mapped_column(
        ForeignKey("scopes.id"), primary_key=True,
    )
    include_descendants: Mapped[bool] = mapped_column(
        Boolean, default=True,
    )
    delegated_permission_keys: Mapped[list | None] = mapped_column(JSON, nullable=True)
    granted_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", name="fk_role_bindings_granted_by"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )


class SecurityAuditEvent(Base):
    """Append-only security-sensitive administration event."""

    __tablename__ = "security_audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    target_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    scope_id: Mapped[str | None] = mapped_column(
        ForeignKey("scopes.id", ondelete="SET NULL"),
    )
    request_id: Mapped[str | None] = mapped_column(String(64))
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(),
    )


class Agent(Base):
    """Enterprise agent entry (design §10, RBAC design §6).

    ``slug`` doubles as the AgentScope agent id fragment; capabilities
    are bound by stable installation id (RBAC design §8).
    """

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("scope_id", "slug"),
        UniqueConstraint("scope_id", "system_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Product-owned hard ceiling. It is copied onto every Product Task so
    # later Agent edits cannot widen an already-running authorization.
    knowledge_config: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    tool_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    # Minimal identity for platform-seeded Agents. The Agent remains a normal
    # scoped resource and receives no authorization from this marker.
    system_key: Mapped[str | None] = mapped_column(String(64), index=True)
    # Selects the Product-backed tool catalog exposed by Runtime. Every call
    # is still authorized in the Product Plane.
    platform_tool_profile: Mapped[str] = mapped_column(
        String(16), default="none", server_default="none",
    )
    # AgentScope-side agent id (server-assigned by POST /agent).
    agentscope_agent_id: Mapped[str | None] = mapped_column(String(64))
    # Logical ownership (RBAC design §6); the runtime owner is resolved
    # from the scope, never stored here.
    scope_id: Mapped[str] = mapped_column(
        ForeignKey("scopes.id"), index=True,
    )
    visibility: Mapped[str] = mapped_column(
        String(16), default=Visibility.PRIVATE.value,
        server_default=Visibility.PRIVATE.value,
    )
    include_descendants: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(),
    )

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    scope: Mapped[Scope] = relationship()
    capability_bindings: Mapped[list["AgentCapabilityBinding"]] = (
        relationship(back_populates="agent")
    )


class AgentDefault(Base):
    """Per-scope default agent (RBAC design §6.1)."""

    __tablename__ = "agent_defaults"

    scope_id: Mapped[str] = mapped_column(
        ForeignKey("scopes.id"), primary_key=True,
    )
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    updated_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(),
    )


class CapabilityInstallation(Base):
    """Product-side installation record for a Skill/MCP (RBAC §8).

    Runtime execution resolves immutable Skill packages and MCP definitions
    from the Product installation identity.
    """

    __tablename__ = "capability_installations"
    # Historical Skill/MCP duplicates survive organization centralization.
    # New names are checked under the organization row lock.

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    capability_type: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[str] = mapped_column(
        ForeignKey("scopes.id"), index=True,
    )
    name: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(),
    )


class ResourceShare(Base):
    """Explicit use grants. Ownership and management permissions never move."""

    __tablename__ = "resource_shares"
    __table_args__ = (
        CheckConstraint("(CASE WHEN agent_id IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN capability_id IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN knowledge_base_id IS NOT NULL THEN 1 ELSE 0 END) = 1", name="ck_resource_share_one_resource"),
        UniqueConstraint("agent_id", "target_scope_id"),
        UniqueConstraint("capability_id", "target_scope_id"),
        UniqueConstraint("knowledge_base_id", "target_scope_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    capability_id: Mapped[str | None] = mapped_column(ForeignKey("capability_installations.id", ondelete="CASCADE"), index=True)
    knowledge_base_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    allow_download: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    target_scope_id: Mapped[str] = mapped_column(ForeignKey("scopes.id", ondelete="CASCADE"), index=True)
    include_descendants: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentCapabilityBinding(Base):
    """Agent <-> capability installation binding by stable id (RBAC §8)."""

    __tablename__ = "agent_capability_bindings"

    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id"), primary_key=True,
    )
    capability_installation_id: Mapped[str] = mapped_column(
        ForeignKey("capability_installations.id"), primary_key=True,
    )

    agent: Mapped[Agent] = relationship(back_populates="capability_bindings")


class Conversation(Base):
    __tablename__ = "conversations"
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (
        CheckConstraint("product_seq >= 0", name="ck_conversation_product_seq"),
        CheckConstraint('business_seq>=0 AND business_seq<=9007199254740991',name='ck_conversation_business_seq'),
    )

    # Browser event watermark, independent from Runtime transport sequencing.
    product_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    business_seq: Mapped[int] = mapped_column(BigInteger, server_default='0')

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), index=True,
    )
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    title: Mapped[str] = mapped_column(String(255), default="New conversation")
    # Product admission pointer; only the delivery gate may clear it.
    active_product_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", name="fk_conversation_active_product_task", use_alter=True),
    )
    # Selected workmate.yaml model profile; NULL = no session yet.
    model_profile: Mapped[str | None] = mapped_column(String(64))
    # Model id actually applied to the runtime session (the profile's
    # model at the time of the last session configuration). Lazy
    # reconcile compares it against the profile's current model after
    # ops change a profile's model id.
    model_profile_applied: Mapped[str | None] = mapped_column(String(64))
    # Fingerprint of the credential revision the session's
    # chat_model_config currently references — key rotations and gateway
    # moves re-bind the session to a new credential revision.
    applied_credential_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
    )
    # The AgentScope session id returned by POST /sessions.
    agentscope_session_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(),
    )

    agent: Mapped[Agent] = relationship()
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="conversation", order_by="Task.created_at",
        foreign_keys="Task.conversation_id",
    )


class ModelCredential(Base):
    """Runtime credential cache per (user, model profile).

    Stores independently managed credentials for multi-model
    deployments: same-provider profiles with different API keys need
    distinct AgentScope credentials. ``config_fingerprint`` is an HMAC
    over the credential's effective configuration (provider, base URL,
    API key) — a mismatch against the current profile creates a new,
    immutable Runtime credential revision. Existing revisions remain
    tracked while sessions may still reference them.
    """

    __tablename__ = "model_credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), index=True,
    )
    profile_name: Mapped[str] = mapped_column(String(64))
    credential_id: Mapped[str] = mapped_column(String(64))
    config_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # active = current revision for the profile config it pins;
    # retiring = superseded, kept until no session references it.
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "profile_name", "config_fingerprint",
            name="uq_model_credentials_user_fingerprint",
        ),
    )


class TaskUsageScope(Base):
    """Task attribution projection, including membership ancestors at creation.

    Scope IDs deliberately survive scope deletion; they are historical facts.
    A Task can appear in several department views but is counted once per view.
    """
    __tablename__ = "task_usage_scopes"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    __table_args__ = (Index("ix_task_usage_scopes_scope_task", "scope_id", "task_id"),)


class Schedule(Base):
    __tablename__ = 'schedules'
    __table_args__ = (
        CheckConstraint("status IN ('active','paused','completed') AND version>0", name='ck_schedule_state'),
        Index('ix_schedules_due', 'next_fire_at', 'id', postgresql_where=text("status='active' AND deleted_at IS NULL")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey('agents.id'))
    name: Mapped[str] = mapped_column(String(80))
    prompt: Mapped[str] = mapped_column(Text)
    model_profile: Mapped[str | None] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(64))
    rule: Mapped[dict] = mapped_column(JSON)
    notification: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    status: Mapped[str] = mapped_column(String(16), default='active', server_default='active')
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ScheduleOccurrence(Base):
    __tablename__ = 'schedule_occurrences'
    __table_args__ = (
        UniqueConstraint('schedule_id', 'scheduled_for', name='uq_schedule_occurrence_due'),
        CheckConstraint("outcome IN ('task_created','skipped_busy','missed','blocked') AND missed_count>=0", name='ck_schedule_occurrence_outcome'),
        Index('ix_schedule_occurrences_history', 'schedule_id', 'scheduled_for'),
        Index('ix_schedule_occurrences_version', 'schedule_id', 'schedule_version'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(ForeignKey('schedules.id'))
    schedule_version: Mapped[int] = mapped_column(Integer)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[str | None] = mapped_column(ForeignKey('tasks.id'), unique=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey('conversations.id'))
    snapshot: Mapped[dict] = mapped_column(JSON)
    missed_count: Mapped[int] = mapped_column(Integer, default=0, server_default='0')


class Task(Base):
    """Product fact of one run (design §12).

    ``run_id`` stores the AgentScope ``reply_id`` (correction #1).
    """

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_created_conversation", "created_at", "conversation_id"),
        Index('ix_tasks_model_unavailable', 'id', postgresql_where=text("error->>'code'='model_unavailable'")),
        CheckConstraint("(origin='user' AND schedule_occurrence_id IS NULL AND channel_inbox_id IS NULL) OR (origin='schedule' AND schedule_occurrence_id IS NOT NULL AND channel_inbox_id IS NULL AND approval_profile='unattended') OR (origin='channel' AND channel_inbox_id IS NOT NULL AND schedule_occurrence_id IS NULL AND approval_profile='standard')", name='ck_task_schedule_origin'))

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), index=True,
    )
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), default=TaskStatus.CREATED.value,
        server_default=TaskStatus.CREATED.value,
        index=True,
    )
    run_id: Mapped[str | None] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(16), default='user', server_default='user')
    channel_inbox_id: Mapped[str | None] = mapped_column(ForeignKey('channel_inbox.id', name='fk_task_channel_inbox', use_alter=True), unique=True)
    schedule_occurrence_id: Mapped[str | None] = mapped_column(ForeignKey('schedule_occurrences.id', name='fk_task_schedule_occurrence', use_alter=True), unique=True)
    approval_profile: Mapped[str] = mapped_column(
        String(16),
        default=ApprovalProfile.STANDARD.value,
        server_default=ApprovalProfile.STANDARD.value,
    )
    # Last durable HITL decision accepted for this parked reply.  It is
    # intentionally Task-scoped and makes retries idempotent.
    last_decision_version: Mapped[str | None] = mapped_column(String(128))
    # Artifact baselines are captured before Runtime dispatch and must survive
    # a product-api restart.  ``artifact_baseline_captured`` distinguishes a
    # real empty baseline (``{}``) from legacy Tasks created before this fact
    # was persisted.
    artifact_output_snapshot: Mapped[dict | None] = mapped_column(JSON)
    artifact_presentation_snapshot: Mapped[str | None] = mapped_column(
        String(64),
    )
    artifact_baseline_captured: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0",
    )
    error: Mapped[dict | None] = mapped_column(JSON)
    # Run metrics (design §13).
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    steps: Mapped[int | None] = mapped_column(Integer)
    model_name: Mapped[str | None] = mapped_column(String(128))
    token_usage: Mapped[dict | None] = mapped_column(JSON)
    # Execution-time pricing; never recomputed from mutable Conversation/config.
    usage_model_profile: Mapped[str | None] = mapped_column(String(64))
    usage_model_id: Mapped[str | None] = mapped_column(String(255))
    usage_pricing: Mapped[dict | None] = mapped_column(JSON)
    usage_currency: Mapped[str | None] = mapped_column(String(3))
    usage_cost: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(),
    )

    conversation: Mapped[Conversation] = relationship(back_populates="tasks", foreign_keys=[conversation_id])
    messages: Mapped[list["TaskMessage"]] = relationship(
        back_populates="task", order_by="TaskMessage.created_at",
    )


class TaskMessage(Base):
    __tablename__ = "task_messages"
    __table_args__ = (
        CheckConstraint('snapshot_version>=0 AND snapshot_business_seq>=0',name='ck_message_snapshot_versions'),
        CheckConstraint("snapshot_version=0 OR (role='assistant' AND snapshot_reply_id IS NOT NULL "
            "AND snapshot_execution_epoch IS NOT NULL AND snapshot_execution_epoch>0 "
            "AND snapshot_phase IS NOT NULL AND snapshot_phase IN ('partial','approval','final','interrupted'))",
            name='ck_message_snapshot_identity'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), index=True,
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[dict] = mapped_column(JSON)  # {text} | {blocks}
    snapshot_version: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    snapshot_reply_id: Mapped[str | None] = mapped_column(String(128))
    snapshot_execution_epoch: Mapped[int | None] = mapped_column(BigInteger)
    covered_cursor: Mapped[str | None] = mapped_column(Text)
    snapshot_business_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    snapshot_phase: Mapped[str | None] = mapped_column(String(24))
    snapshot_incomplete: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    task: Mapped[Task] = relationship(back_populates="messages")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), index=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(128))
    storage_key: Mapped[str] = mapped_column(String(512))
    size: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Revision creation kind; repeated delivery lives in TaskArtifactDelivery.
    change_type: Mapped[str] = mapped_column(
        String(16),
    )  # created | modified | presented
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )


class TaskArtifactDelivery(Base):
    """A Task's delivery of an immutable Artifact revision."""
    __tablename__ = "task_artifact_deliveries"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), primary_key=True, index=True)
    change_type: Mapped[str] = mapped_column(String(16))
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskEvent(Base):
    """Append-only event log (design §13/§21).

    Lifecycle, HITL, artifacts and whitelisted tool boundary metadata only.
    Text deltas and full tool inputs/results never land here.
    HITL retains original calls for recovery. Tool Audit filters this table.
    """

    __tablename__ = "task_events"
    __table_args__ = (
        # Unique: seq is allocated under the Task row lock; the
        # constraint turns an allocator regression into a hard error
        # instead of silent audit-order corruption.
        # See EventConsumer._allocate_seq.
        Index("uq_task_events_task_seq", "task_id", "seq", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), index=True,
    )
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )


class ToolExecutionReceipt(Base):
    """Durable idempotency record for Product-backed Runtime tools."""

    __tablename__ = "tool_execution_receipts"
    __table_args__ = (
        UniqueConstraint("task_id", "tool_call_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending",
    )
    response: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(),
    )


class KnowledgeBase(Base):
    """Product ownership; Runtime owns documents and indexing state."""
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_id: Mapped[str] = mapped_column(ForeignKey("scopes.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    runtime_id: Mapped[str] = mapped_column(String(64), unique=True)
    runtime_owner_id: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KnowledgeDocumentUpload(Base):
    """Product upload/explicit-admission ledger, not a Runtime indexing state."""
    __tablename__ = "knowledge_document_uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(150))
    size: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="uploading")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# Role-neutral persistence tables. Runtime events are transport records, not
# a second Product audit log. Product business audit remains task_events.
_RUNTIME_JSON = JSON().with_variant(JSONB(), "postgresql")


class RuntimeSession(Base):
    __tablename__ = "runtime_sessions"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    storage_node_id: Mapped[str] = mapped_column(String(128))
    active_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    owner_id: Mapped[str | None] = mapped_column(String(128))
    execution_epoch: Mapped[int] = mapped_column(BigInteger, server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_version: Mapped[int] = mapped_column(BigInteger, server_default="0")
    runtime_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    status: Mapped[str] = mapped_column(String(32), server_default="accepted")
    snapshot: Mapped[dict | None] = mapped_column(_RUNTIME_JSON)
    snapshot_id: Mapped[str | None] = mapped_column(String(128))
    reply_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("uq_runtime_sessions_current_conversation", "conversation_id", unique=True,
            postgresql_where=text("superseded_at IS NULL"), sqlite_where=text("superseded_at IS NULL")),
        ForeignKeyConstraint(["snapshot_id", "id"], ["runtime_snapshots.id", "runtime_snapshots.session_id"],
                             name="fk_runtime_session_snapshot", use_alter=True),
        CheckConstraint("execution_epoch >= 0 AND state_version >= 0 AND runtime_seq >= 0", name="ck_runtime_session_versions"),
        CheckConstraint("status IN ('accepted','preparing','running','awaiting_interaction','stopping','completed','failed','cancelled','interrupted')", name="ck_runtime_session_status"),
        Index("ix_runtime_sessions_storage_status", "storage_node_id", "status"),
    )


class RuntimeCommand(Base):
    __tablename__ = "runtime_commands"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("runtime_sessions.id"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    kind: Mapped[str] = mapped_column(String(32))
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_epoch: Mapped[int | None] = mapped_column(BigInteger)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_epoch: Mapped[int] = mapped_column(BigInteger, server_default="0")
    status: Mapped[str] = mapped_column(String(32), server_default="accepted")
    payload: Mapped[dict] = mapped_column(_RUNTIME_JSON)
    protocol_version: Mapped[int] = mapped_column(Integer, server_default="1")
    content_digest: Mapped[str] = mapped_column(String(64))
    reply_id: Mapped[str | None] = mapped_column(String(128))
    interaction_id: Mapped[str | None] = mapped_column(String(128))
    request_version: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("expected_epoch >= 0 AND protocol_version >= 1", name="ck_runtime_command_versions"),
        CheckConstraint("kind != 'StartTask' OR task_id IS NOT NULL", name="ck_runtime_start_task_identity"),
        CheckConstraint("kind IN ('PrepareSession','StartTask','ResumeInteraction','StopTask')", name="ck_runtime_command_kind"),
        CheckConstraint("status IN ('accepted','claimed','completed','rejected','indeterminate')", name="ck_runtime_command_status"),
        CheckConstraint("claimed_epoch IS NULL OR claimed_epoch > 0", name="ck_runtime_claimed_epoch"),
        CheckConstraint("length(content_digest) = 64", name="ck_runtime_command_digest"),
        Index("ix_runtime_commands_session_status", "session_id", "status"),
        Index("uq_runtime_commands_start_task", "task_id", unique=True,
              postgresql_where=text("kind = 'StartTask'"), sqlite_where=text("kind = 'StartTask'")),
    )


class RuntimeEvent(Base):
    __tablename__ = "runtime_events"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("runtime_sessions.id"))
    runtime_seq: Mapped[int] = mapped_column(BigInteger)
    execution_epoch: Mapped[int] = mapped_column(BigInteger)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    payload: Mapped[dict] = mapped_column(_RUNTIME_JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("session_id", "runtime_seq", name="uq_runtime_events_session_seq"),
        CheckConstraint("runtime_seq > 0 AND execution_epoch > 0", name="ck_runtime_event_versions"),
    )


class RuntimeSnapshot(Base):
    """Immutable Core boundary; payload includes the versioned CoreSnapshot envelope."""
    __tablename__ = "runtime_snapshots"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("runtime_sessions.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    state_version: Mapped[int] = mapped_column(BigInteger)
    runtime_seq: Mapped[int] = mapped_column(BigInteger)
    execution_epoch: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32))
    reply_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(_RUNTIME_JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("session_id", "state_version", name="uq_runtime_snapshot_session_version"),
        UniqueConstraint("id", "session_id", name="uq_runtime_snapshot_identity_session"),
        UniqueConstraint("id", "task_id", "session_id", "state_version", name="uq_runtime_snapshot_boundary"),
        CheckConstraint("state_version > 0 AND runtime_seq > 0 AND execution_epoch > 0", name="ck_runtime_snapshot_versions"),
        CheckConstraint("status IN ('awaiting_interaction','completed','failed','cancelled','interrupted')", name="ck_runtime_snapshot_status"),
    )


class RuntimeTaskExecution(Base):
    """Execution state of an existing Product Task, never a Product Run entity."""
    __tablename__ = "runtime_task_executions"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("runtime_sessions.id"))
    status: Mapped[str] = mapped_column(String(32), server_default="accepted")
    execution_epoch: Mapped[int] = mapped_column(BigInteger, server_default="0")
    state_version: Mapped[int] = mapped_column(BigInteger, server_default="0")
    reply_id: Mapped[str | None] = mapped_column(String(128))
    snapshot_id: Mapped[str | None] = mapped_column(String(128))
    input_snapshot_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(["snapshot_id", "task_id", "session_id", "state_version"],
                             ["runtime_snapshots.id", "runtime_snapshots.task_id", "runtime_snapshots.session_id", "runtime_snapshots.state_version"], name="fk_runtime_execution_snapshot"),
        ForeignKeyConstraint(["input_snapshot_id", "session_id"],
                             ["runtime_snapshots.id", "runtime_snapshots.session_id"], name="fk_runtime_execution_input_snapshot"),
        CheckConstraint("execution_epoch >= 0 AND state_version >= 0", name="ck_runtime_execution_versions"),
        CheckConstraint("status IN ('accepted','preparing','running','awaiting_interaction','stopping','completed','failed','cancelled','interrupted')", name="ck_runtime_execution_status"),
        Index("ix_runtime_executions_session", "session_id"),
    )


# create_all is used by isolated tests; keep its immutability boundary identical
# to the explicit Alembic migration. No application startup invokes this DDL.
for operation in ("UPDATE", "DELETE"):
    event.listen(RuntimeSnapshot.__table__, "after_create", DDL(
        f"CREATE TRIGGER runtime_snapshot_no_{operation.lower()} BEFORE {operation} ON runtime_snapshots "
        "BEGIN SELECT RAISE(ABORT, 'runtime snapshots are immutable'); END"
    ).execute_if(dialect="sqlite"))
event.listen(RuntimeSnapshot.__table__, "after_create", DDL(
    "CREATE FUNCTION reject_runtime_snapshot_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "BEGIN RAISE EXCEPTION 'runtime snapshots are immutable'; END $$"
).execute_if(dialect="postgresql"))
event.listen(RuntimeSnapshot.__table__, "after_create", DDL(
    "CREATE TRIGGER runtime_snapshot_immutable BEFORE UPDATE OR DELETE ON runtime_snapshots "
    "FOR EACH ROW EXECUTE FUNCTION reject_runtime_snapshot_mutation()"
).execute_if(dialect="postgresql"))
event.listen(RuntimeSnapshot.__table__, "after_drop", DDL(
    "DROP FUNCTION reject_runtime_snapshot_mutation()"
).execute_if(dialect="postgresql"))


class RuntimeProjectionCursor(Base):
    """Committed Runtime ingestion watermark; writers lock Conversation briefly."""
    __tablename__ = "runtime_projection_cursors"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("runtime_sessions.id"), unique=True)
    last_runtime_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    version: Mapped[int] = mapped_column(BigInteger, server_default="0")
    __table_args__ = (
        CheckConstraint("last_runtime_seq >= 0 AND version >= 0", name="ck_runtime_projection_cursor_versions"),
    )


class ProductEvent(Base):
    """Browser replay transport. Task audit facts remain exclusively task_events."""
    __tablename__ = "product_events"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    product_seq: Mapped[int] = mapped_column(BigInteger)
    business_seq: Mapped[int|None] = mapped_column(BigInteger)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    reply_id: Mapped[str | None] = mapped_column(String(128))
    source_runtime_event_id: Mapped[str | None] = mapped_column(ForeignKey("runtime_events.id"))
    projection_item_key: Mapped[str] = mapped_column(String(128))
    business_operation_key: Mapped[str | None] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(_RUNTIME_JSON)
    __table_args__ = (
        CheckConstraint("product_seq > 0", name="ck_product_event_seq"),
        UniqueConstraint("conversation_id", "product_seq", name="uq_product_events_conversation_seq"),
        UniqueConstraint('conversation_id','business_seq',name='uq_product_events_business_seq'),
        CheckConstraint('business_seq IS NULL OR (business_seq>0 AND business_seq<=9007199254740991)',name='ck_product_event_business_seq'),
        UniqueConstraint("source_runtime_event_id", "projection_item_key", name="uq_product_events_runtime_item"),
        UniqueConstraint("conversation_id", "business_operation_key", "projection_item_key", name="uq_product_events_business_item"),
    )


class TaskProjectionState(Base):
    """Versioned translator/message state committed with the ingestion cursor."""
    __tablename__ = "task_projection_states"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("runtime_sessions.id"))
    reply_id: Mapped[str | None] = mapped_column(String(128))
    format_version: Mapped[int] = mapped_column(Integer, server_default="1")
    state: Mapped[dict] = mapped_column(_RUNTIME_JSON, server_default="{}")
    last_runtime_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    __table_args__ = (
        CheckConstraint("format_version = 1", name="ck_task_projection_format"),
        CheckConstraint("last_runtime_seq >= 0", name="ck_task_projection_sequence"),
    )


class RuntimeExecutionSpec(Base):
    """Immutable trusted Product admission descriptor; never a credential store."""
    __tablename__ = 'runtime_execution_specs'
    task_id: Mapped[str] = mapped_column(ForeignKey('tasks.id'),primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('runtime_sessions.id'))
    schema_version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(_RUNTIME_JSON)
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),server_default=func.now())
    __table_args__ = (
        CheckConstraint('schema_version = 1',name='ck_runtime_execution_spec_version'),
        CheckConstraint('length(digest) = 64',name='ck_runtime_execution_spec_digest'),
    )


for operation in ('UPDATE','DELETE'):
    event.listen(RuntimeExecutionSpec.__table__,'after_create',DDL(
        f'CREATE TRIGGER runtime_execution_spec_no_{operation.lower()} BEFORE {operation} ON runtime_execution_specs '
        "BEGIN SELECT RAISE(ABORT, 'runtime execution specs are immutable'); END"
    ).execute_if(dialect='sqlite'))
event.listen(RuntimeExecutionSpec.__table__,'after_create',DDL(
    "CREATE FUNCTION reject_runtime_execution_spec_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "BEGIN RAISE EXCEPTION 'runtime execution specs are immutable'; END $$"
).execute_if(dialect='postgresql'))
event.listen(RuntimeExecutionSpec.__table__,'after_create',DDL(
    'CREATE TRIGGER runtime_execution_spec_immutable BEFORE UPDATE OR DELETE ON runtime_execution_specs '
    'FOR EACH ROW EXECUTE FUNCTION reject_runtime_execution_spec_mutation()'
).execute_if(dialect='postgresql'))
event.listen(RuntimeExecutionSpec.__table__,'after_drop',DDL(
    'DROP FUNCTION reject_runtime_execution_spec_mutation()'
).execute_if(dialect='postgresql'))


class SkillArtifactRecord(Base):
    """Validated Skill archive shared by Workers, pinned by content digest."""
    __tablename__='skill_artifacts'
    digest: Mapped[str] = mapped_column(String(64),primary_key=True)
    format_version: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    manifest: Mapped[list] = mapped_column(_RUNTIME_JSON)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),server_default=func.now())
    __table_args__=(
        CheckConstraint('format_version = 1',name='ck_skill_artifact_format'),
        CheckConstraint('length(digest) = 64',name='ck_skill_artifact_digest'),
        CheckConstraint('size_bytes > 0 AND size_bytes <= 20971520',name='ck_skill_artifact_size'),
        CheckConstraint("storage_key = 'sha256/' || substr(digest, 1, 2) || '/' || digest || '.zip'",
                        name='ck_skill_artifact_location'),
    )


class CapabilitySkillArtifact(Base):
    """Presence is the ready binding; a new revision affects future Tasks only."""
    __tablename__='capability_skill_artifacts'
    capability_id: Mapped[str] = mapped_column(ForeignKey('capability_installations.id',ondelete='CASCADE'),primary_key=True)
    artifact_digest: Mapped[str] = mapped_column(ForeignKey('skill_artifacts.digest'))


for operation in ('UPDATE','DELETE'):
    event.listen(SkillArtifactRecord.__table__,'after_create',DDL(
        f'CREATE TRIGGER skill_artifact_no_{operation.lower()} BEFORE {operation} ON skill_artifacts '
        "BEGIN SELECT RAISE(ABORT, 'skill artifacts are immutable'); END"
    ).execute_if(dialect='sqlite'))
event.listen(SkillArtifactRecord.__table__,'after_create',DDL(
    "CREATE FUNCTION reject_skill_artifact_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "BEGIN RAISE EXCEPTION 'skill artifacts are immutable'; END $$"
).execute_if(dialect='postgresql'))
event.listen(SkillArtifactRecord.__table__,'after_create',DDL(
    'CREATE TRIGGER skill_artifact_immutable BEFORE UPDATE OR DELETE ON skill_artifacts '
    'FOR EACH ROW EXECUTE FUNCTION reject_skill_artifact_mutation()'
).execute_if(dialect='postgresql'))
event.listen(SkillArtifactRecord.__table__,'after_drop',DDL(
    'DROP FUNCTION reject_skill_artifact_mutation()'
).execute_if(dialect='postgresql'))


class _DeliveryColumns:
    task_id: Mapped[str] = mapped_column(ForeignKey('tasks.id'),primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('runtime_sessions.id'))
    execution_epoch: Mapped[int] = mapped_column(BigInteger)
    worker_id: Mapped[str] = mapped_column(String(128))
    storage_node_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(_RUNTIME_JSON)
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),server_default=func.now())


class RuntimeDeliveryBaseline(_DeliveryColumns,Base):
    __tablename__='runtime_delivery_baselines'
    __table_args__=(CheckConstraint('execution_epoch > 0',name='ck_runtime_delivery_baselines_epoch'),
        CheckConstraint('length(digest) = 64',name='ck_runtime_delivery_baselines_digest'))


class RuntimeDeliveryCapture(_DeliveryColumns,Base):
    __tablename__='runtime_delivery_captures'
    snapshot_id: Mapped[str] = mapped_column(ForeignKey('runtime_snapshots.id'))
    __table_args__=(CheckConstraint('execution_epoch > 0',name='ck_runtime_delivery_captures_epoch'),
        CheckConstraint('length(digest) = 64',name='ck_runtime_delivery_captures_digest'))


class RuntimeDeliverySeal(_DeliveryColumns,Base):
    __tablename__='runtime_delivery_seals'
    __table_args__=(CheckConstraint('execution_epoch > 0',name='ck_runtime_delivery_seals_epoch'),
        CheckConstraint('length(digest) = 64',name='ck_runtime_delivery_seals_digest'))


for delivery_model in (RuntimeDeliveryBaseline,RuntimeDeliveryCapture,RuntimeDeliverySeal):
    delivery_table=delivery_model.__tablename__
    for operation in ('UPDATE','DELETE'):
        event.listen(delivery_model.__table__,'after_create',DDL(
            f'CREATE TRIGGER {delivery_table}_no_{operation.lower()} BEFORE {operation} ON {delivery_table} '
            "BEGIN SELECT RAISE(ABORT, 'delivery records are immutable'); END").execute_if(dialect='sqlite'))
    event.listen(delivery_model.__table__,'after_create',DDL(
        f"CREATE FUNCTION reject_{delivery_table}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'delivery records are immutable'; END $$").execute_if(dialect='postgresql'))
    event.listen(delivery_model.__table__,'after_create',DDL(
        f'CREATE TRIGGER {delivery_table}_immutable BEFORE UPDATE OR DELETE ON {delivery_table} '
        f'FOR EACH ROW EXECUTE FUNCTION reject_{delivery_table}_mutation()').execute_if(dialect='postgresql'))
    event.listen(delivery_model.__table__,'after_drop',DDL(
        f'DROP FUNCTION reject_{delivery_table}_mutation()').execute_if(dialect='postgresql'))

# Register split permission models with the same Alembic/application metadata.
from .full_access_models import RuntimeSandboxAttestation, RuntimePermissionUpgrade  # noqa: E402,F401

class SkillUploadSource(Base):
    """Governed uploaded catalog entries survive uninstalling an installation."""
    __tablename__='skill_upload_sources'
    scope_id: Mapped[str] = mapped_column(ForeignKey('scopes.id'),primary_key=True)
    card_id: Mapped[str] = mapped_column(String(64),primary_key=True)
    artifact_digest: Mapped[str] = mapped_column(ForeignKey('skill_artifacts.digest'))
    uploaded_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    card: Mapped[dict] = mapped_column(_RUNTIME_JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),server_default=func.now())


# Register private MCP revisions in the shared migration metadata.
from .mcp_models import MCPSecretRevision, CapabilityMCPRevision  # noqa: E402,F401

# Register immutable external result receipts.
from .external_result_models import RuntimeExternalReceipt  # noqa: E402,F401
from .memory_models import RuntimeMemoryNamespace  # noqa: E402,F401
from .node_recovery_models import RuntimeResourceLaunchResolution, RuntimeWorkerIncarnation, RuntimeResourceLedger, RuntimeResourceRegistration, RuntimeNodeQuiescenceEvidence  # noqa: E402,F401

from .retirement_models import ConversationRetirement  # noqa: E402,F401

from .suspension_models import RuntimeSuspension  # noqa: E402,F401
from .command_delivery_models import CommandDelivery, CommandDeliveryQuarantine  # noqa: E402,F401

from .workspace_residency_models import WorkspaceResidency, WorkspaceReleaseRequest  # noqa: E402,F401
from .realtime_models import ConversationEventLog, RealtimeBusinessOutbox  # noqa: E402,F401
from .checkpoint_models import WorkspaceCheckpoint  # noqa: E402,F401
from .recovery_forks import RecoveryFork  # noqa: E402,F401

# An occurrence may bind its freshly admitted Task once; its authority snapshot
# and settled trigger outcome never change, including after Schedule edits.
event.listen(ScheduleOccurrence.__table__, 'after_create', DDL("""
    CREATE FUNCTION reject_schedule_occurrence_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'schedule occurrence admissions are immutable'; END IF;
        IF (to_jsonb(NEW)-'task_id'-'conversation_id') IS DISTINCT FROM
           (to_jsonb(OLD)-'task_id'-'conversation_id')
           OR (OLD.task_id IS NOT NULL AND NEW.task_id IS DISTINCT FROM OLD.task_id)
           OR (OLD.conversation_id IS NOT NULL AND NEW.conversation_id IS DISTINCT FROM OLD.conversation_id)
        THEN RAISE EXCEPTION 'schedule occurrence admissions are immutable'; END IF;
        RETURN NEW;
    END $$
""").execute_if(dialect='postgresql'))
event.listen(ScheduleOccurrence.__table__, 'after_create', DDL(
    'CREATE TRIGGER schedule_occurrences_immutable BEFORE UPDATE OR DELETE ON schedule_occurrences '
    'FOR EACH ROW EXECUTE FUNCTION reject_schedule_occurrence_mutation()').execute_if(dialect='postgresql'))
event.listen(ScheduleOccurrence.__table__, 'after_drop', DDL(
    'DROP FUNCTION reject_schedule_occurrence_mutation()').execute_if(dialect='postgresql'))
event.listen(ScheduleOccurrence.__table__, 'after_create', DDL(
    "CREATE TRIGGER schedule_occurrences_no_delete BEFORE DELETE ON schedule_occurrences "
    "BEGIN SELECT RAISE(ABORT, 'schedule occurrence admissions are immutable'); END").execute_if(dialect='sqlite'))
_schedule_fixed_columns = ('id','schedule_id','schedule_version','scheduled_for','triggered_at','outcome','reason','snapshot','missed_count')
event.listen(ScheduleOccurrence.__table__, 'after_create', DDL(
    'CREATE TRIGGER schedule_occurrences_immutable BEFORE UPDATE ON schedule_occurrences WHEN '
    + ' OR '.join(f'NEW.{column} IS NOT OLD.{column}' for column in _schedule_fixed_columns)
    + ' OR (OLD.task_id IS NOT NULL AND NEW.task_id IS NOT OLD.task_id)'
    + ' OR (OLD.conversation_id IS NOT NULL AND NEW.conversation_id IS NOT OLD.conversation_id)'
    + " BEGIN SELECT RAISE(ABORT, 'schedule occurrence admissions are immutable'); END").execute_if(dialect='sqlite'))

from .channel_models import ChannelConnection, ChannelIdentity, ChannelBindingCode, ChannelConversation, ChannelInbox, ChannelOutbox  # noqa: E402,F401
