"""Create the Workmate database schema.

This immutable baseline targets an empty PostgreSQL schema. Future changes must
use new revisions; never import application models into this migration.
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Workmate migrations require PostgreSQL")
    if not op.get_context().as_sql:
        objects = op.get_bind().execute(sa.text("""
            SELECT c.relname FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
              AND c.relname <> 'alembic_version'
        """)).scalars().all()
        if objects:
            raise RuntimeError("Initial schema requires an empty database schema; "
                               "existing objects: " + ", ".join(sorted(objects)))
    for statement in SCHEMA_STATEMENTS:
        op.execute(sa.text(statement.replace(":", r"\:")))
    for statement in SEED_STATEMENTS:
        op.execute(sa.text(statement.replace(":", r"\:")))


def downgrade() -> None:
    raise RuntimeError("The initial schema cannot be downgraded; restore a backup "
                       "or create a separate empty database")


SCHEMA_STATEMENTS = (
    """CREATE FUNCTION protect_conversation_retirement() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'retirement requests are retained'; END IF;
 IF (to_jsonb(NEW) - 'completed_at' - 'completed_by' - 'purged_at' - 'purged_by') IS DISTINCT FROM
    (to_jsonb(OLD) - 'completed_at' - 'completed_by' - 'purged_at' - 'purged_by') OR
    (OLD.completed_at IS NOT NULL AND (NEW.completed_at IS DISTINCT FROM OLD.completed_at OR NEW.completed_by IS DISTINCT FROM OLD.completed_by)) OR
    (OLD.purged_at IS NOT NULL AND NEW IS DISTINCT FROM OLD) OR
    (NEW.completed_at IS NULL) <> (NEW.completed_by IS NULL) OR
    (NEW.purged_at IS NULL) <> (NEW.purged_by IS NULL) OR
    (NEW.purged_at IS NOT NULL AND NEW.completed_at IS NULL)
 THEN RAISE EXCEPTION 'retirement identity and receipts are immutable'; END IF;
 RETURN NEW;
END $$""",

    """CREATE FUNCTION protect_conversation_tombstone() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
 IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
 THEN RAISE EXCEPTION 'conversation retirement cannot be reversed'; END IF;
 RETURN NEW;
END $$""",

    """CREATE FUNCTION protect_runtime_resource_ledger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN IF TG_OP='DELETE' THEN RAISE EXCEPTION 'resource ledgers cannot be deleted'; END IF; IF OLD.closed OR OLD.task_id IS DISTINCT FROM NEW.task_id OR OLD.execution_epoch IS DISTINCT FROM NEW.execution_epoch OR OLD.worker_id IS DISTINCT FROM NEW.worker_id OR OLD.session_id IS DISTINCT FROM NEW.session_id OR OLD.workspace IS DISTINCT FROM NEW.workspace OR OLD.workspace_digest IS DISTINCT FROM NEW.workspace_digest OR OLD.created_at IS DISTINCT FROM NEW.created_at OR (NOT NEW.closed AND NEW.resource_seq<>OLD.resource_seq+1) OR (NEW.closed AND NEW.resource_seq<>OLD.resource_seq) THEN RAISE EXCEPTION 'resource ledger identity and closure are immutable'; END IF; RETURN NEW; END $$""",

    """CREATE FUNCTION reject_channel_inbox_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
IF TG_OP='DELETE' THEN RAISE EXCEPTION 'channel inbox provenance is immutable'; END IF;
IF (to_jsonb(NEW)-'status'-'error'-'user_id'-'conversation_id'-'task_id') IS DISTINCT FROM
   (to_jsonb(OLD)-'status'-'error'-'user_id'-'conversation_id'-'task_id')
   OR (OLD.user_id IS NOT NULL AND NEW.user_id IS DISTINCT FROM OLD.user_id)
   OR (OLD.conversation_id IS NOT NULL AND NEW.conversation_id IS DISTINCT FROM OLD.conversation_id)
   OR (OLD.task_id IS NOT NULL AND NEW.task_id IS DISTINCT FROM OLD.task_id)
THEN RAISE EXCEPTION 'channel inbox provenance is immutable'; END IF;
RETURN NEW;
END $$""",

    """CREATE FUNCTION reject_external_receipt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            BEGIN
              IF TG_OP='DELETE' AND EXISTS (SELECT 1 FROM conversation_retirements r
        JOIN conversations c ON c.id=r.conversation_id
        JOIN runtime_sessions s ON s.id=r.session_id
        WHERE r.session_id=OLD.session_id AND r.completed_at IS NOT NULL
        AND r.purged_at IS NULL AND c.deleted_at IS NOT NULL
        AND c.active_product_task_id IS NULL AND s.active_task_id IS NULL) THEN RETURN OLD; END IF;
              RAISE EXCEPTION 'execution content is immutable until retired';
            END $$""",

    """CREATE FUNCTION reject_mcp_secret_revision_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'MCP revisions are immutable'; END $$""",

    """CREATE FUNCTION reject_runtime_delivery_baselines_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            BEGIN
              IF TG_OP='DELETE' AND EXISTS (SELECT 1 FROM conversation_retirements r
        JOIN conversations c ON c.id=r.conversation_id
        JOIN runtime_sessions s ON s.id=r.session_id
        WHERE r.session_id=OLD.session_id AND r.completed_at IS NOT NULL
        AND r.purged_at IS NULL AND c.deleted_at IS NOT NULL
        AND c.active_product_task_id IS NULL AND s.active_task_id IS NULL) THEN RETURN OLD; END IF;
              RAISE EXCEPTION 'execution content is immutable until retired';
            END $$""",

    """CREATE FUNCTION reject_runtime_delivery_captures_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            BEGIN
              IF TG_OP='DELETE' AND EXISTS (SELECT 1 FROM conversation_retirements r
        JOIN conversations c ON c.id=r.conversation_id
        JOIN runtime_sessions s ON s.id=r.session_id
        WHERE r.session_id=OLD.session_id AND r.completed_at IS NOT NULL
        AND r.purged_at IS NULL AND c.deleted_at IS NOT NULL
        AND c.active_product_task_id IS NULL AND s.active_task_id IS NULL) THEN RETURN OLD; END IF;
              RAISE EXCEPTION 'execution content is immutable until retired';
            END $$""",

    """CREATE FUNCTION reject_runtime_delivery_seals_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            BEGIN
              IF TG_OP='DELETE' AND EXISTS (SELECT 1 FROM conversation_retirements r
        JOIN conversations c ON c.id=r.conversation_id
        JOIN runtime_sessions s ON s.id=r.session_id
        WHERE r.session_id=OLD.session_id AND r.completed_at IS NOT NULL
        AND r.purged_at IS NULL AND c.deleted_at IS NOT NULL
        AND c.active_product_task_id IS NULL AND s.active_task_id IS NULL) THEN RETURN OLD; END IF;
              RAISE EXCEPTION 'execution content is immutable until retired';
            END $$""",

    """CREATE FUNCTION reject_runtime_execution_spec_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            BEGIN
              IF TG_OP='DELETE' AND EXISTS (SELECT 1 FROM conversation_retirements r
        JOIN conversations c ON c.id=r.conversation_id
        JOIN runtime_sessions s ON s.id=r.session_id
        WHERE r.session_id=OLD.session_id AND r.completed_at IS NOT NULL
        AND r.purged_at IS NULL AND c.deleted_at IS NOT NULL
        AND c.active_product_task_id IS NULL AND s.active_task_id IS NULL) THEN RETURN OLD; END IF;
              RAISE EXCEPTION 'execution content is immutable until retired';
            END $$""",

    """CREATE FUNCTION reject_runtime_node_quiescence_evidence_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'node evidence records are immutable'; END $$""",

    """CREATE FUNCTION reject_runtime_permission_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'runtime permission records are immutable'; END $$""",

    """CREATE FUNCTION reject_runtime_resource_launch_resolutions_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'node evidence records are immutable'; END $$""",

    """CREATE FUNCTION reject_runtime_resource_registrations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'node evidence records are immutable'; END $$""",

    """CREATE FUNCTION reject_runtime_snapshot_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            BEGIN
              IF TG_OP='DELETE' AND EXISTS (SELECT 1 FROM conversation_retirements r
        JOIN conversations c ON c.id=r.conversation_id
        JOIN runtime_sessions s ON s.id=r.session_id
        WHERE r.session_id=OLD.session_id AND r.completed_at IS NOT NULL
        AND r.purged_at IS NULL AND c.deleted_at IS NOT NULL
        AND c.active_product_task_id IS NULL AND s.active_task_id IS NULL) THEN RETURN OLD; END IF;
              RAISE EXCEPTION 'execution content is immutable until retired';
            END $$""",

    """CREATE FUNCTION reject_runtime_worker_incarnations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'node evidence records are immutable'; END $$""",

    """CREATE FUNCTION reject_schedule_occurrence_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP='DELETE' THEN RAISE EXCEPTION 'schedule occurrence admissions are immutable'; END IF;
            IF (to_jsonb(NEW)-'task_id'-'conversation_id') IS DISTINCT FROM
               (to_jsonb(OLD)-'task_id'-'conversation_id')
               OR (OLD.task_id IS NOT NULL AND NEW.task_id IS DISTINCT FROM OLD.task_id)
               OR (OLD.conversation_id IS NOT NULL AND NEW.conversation_id IS DISTINCT FROM OLD.conversation_id)
            THEN RAISE EXCEPTION 'schedule occurrence admissions are immutable'; END IF;
            RETURN NEW;
        END $$""",

    """CREATE FUNCTION reject_skill_artifact_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'skill artifacts are immutable'; END $$""",

    """CREATE TABLE agent_capability_bindings (
    agent_id character varying(36) NOT NULL,
    capability_installation_id character varying(36) NOT NULL
)""",

    """CREATE TABLE agent_defaults (
    scope_id character varying(36) NOT NULL,
    agent_id character varying(36) NOT NULL,
    updated_by character varying(36) NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE agents (
    id character varying(36) NOT NULL,
    slug character varying(64) NOT NULL,
    display_name character varying(100) NOT NULL,
    system_prompt text NOT NULL,
    agentscope_agent_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    scope_id character varying(36) NOT NULL,
    visibility character varying(8) DEFAULT 'private'::character varying NOT NULL,
    include_descendants boolean DEFAULT true NOT NULL,
    created_by character varying(36),
    tool_policy json DEFAULT '{}'::json NOT NULL,
    system_key character varying(64),
    platform_tool_profile character varying(16) DEFAULT 'none'::character varying NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    deleted_at timestamp with time zone,
    knowledge_config json DEFAULT '{}'::json NOT NULL
)""",

    """CREATE TABLE artifacts (
    id character varying(36) NOT NULL,
    task_id character varying(36) NOT NULL,
    conversation_id character varying(36) NOT NULL,
    name character varying(255) NOT NULL,
    mime_type character varying(128) NOT NULL,
    storage_key character varying(512) NOT NULL,
    size integer NOT NULL,
    version integer NOT NULL,
    change_type character varying(16) NOT NULL,
    metadata json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE capability_installations (
    id character varying(36) NOT NULL,
    capability_type character varying(8) NOT NULL,
    scope_id character varying(36) NOT NULL,
    name character varying(128) NOT NULL,
    display_name character varying(200),
    enabled boolean DEFAULT true NOT NULL,
    metadata json NOT NULL,
    created_by character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE capability_mcp_revisions (
    capability_id character varying(36) NOT NULL,
    secret_version character varying(36) NOT NULL
)""",

    """CREATE TABLE capability_skill_artifacts (
    capability_id character varying(36) NOT NULL,
    artifact_digest character varying(64) NOT NULL
)""",

    """CREATE TABLE channel_binding_codes (
    code_hash character varying(64) NOT NULL,
    channel_id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone
)""",

    """CREATE TABLE channel_connections (
    id character varying(36) NOT NULL,
    name character varying(100) NOT NULL,
    provider character varying(16) NOT NULL,
    agent_id character varying(36) NOT NULL,
    scope_id character varying(36) NOT NULL,
    owner_user_id character varying(36),
    credentials json DEFAULT '{}'::json NOT NULL,
    bot_key character varying(128),
    enabled boolean DEFAULT false NOT NULL,
    group_enabled boolean DEFAULT false NOT NULL,
    group_allowlist json DEFAULT '[]'::json NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    status character varying(24) DEFAULT 'stopped'::character varying NOT NULL,
    last_error text,
    deleted_at timestamp with time zone,
    lease_owner character varying(128),
    lease_until timestamp with time zone,
    lease_epoch bigint DEFAULT '0'::bigint NOT NULL,
    cursor text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_channel_connection CHECK ((((provider)::text = ANY ((ARRAY['wecom'::character varying, 'feishu'::character varying, 'dingtalk'::character varying, 'wechat'::character varying])::text[])) AND (version > 0) AND (lease_epoch >= 0)))
)""",

    """CREATE TABLE channel_conversations (
    id character varying(36) NOT NULL,
    channel_id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    agent_id character varying(36) NOT NULL,
    chat_key character varying(512) NOT NULL,
    conversation_id character varying(36) NOT NULL
)""",

    """CREATE TABLE channel_identities (
    id character varying(36) NOT NULL,
    channel_id character varying(36) NOT NULL,
    external_user_id character varying(256) NOT NULL,
    user_id character varying(36) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE channel_inbox (
    id character varying(36) NOT NULL,
    channel_id character varying(36) NOT NULL,
    channel_version integer NOT NULL,
    message_id character varying(256) NOT NULL,
    sender_id character varying(256) NOT NULL,
    chat_id character varying(256) NOT NULL,
    chat_type character varying(16) NOT NULL,
    text text NOT NULL,
    target json NOT NULL,
    mentioned boolean DEFAULT false NOT NULL,
    unsupported boolean DEFAULT false NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    user_id character varying(36),
    conversation_id character varying(36),
    task_id character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    error text,
    CONSTRAINT ck_channel_inbox_state CHECK ((((status)::text = ANY ((ARRAY['pending'::character varying, 'processed'::character varying, 'ignored'::character varying, 'rejected'::character varying])::text[])) AND ((chat_type)::text = ANY ((ARRAY['private'::character varying, 'group'::character varying])::text[]))))
)""",

    """CREATE TABLE channel_outbox (
    id character varying(36) NOT NULL,
    channel_id character varying(36) NOT NULL,
    inbox_id character varying(36),
    task_id character varying(36),
    user_id character varying(36),
    kind character varying(32) NOT NULL,
    dedupe_key character varying(256) NOT NULL,
    target json NOT NULL,
    text text NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    lease_owner character varying(128),
    lease_until timestamp with time zone,
    platform_message_id character varying(256),
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone,
    reply_revision integer DEFAULT 1 NOT NULL,
    reply_finished boolean DEFAULT false NOT NULL,
    reply_state json DEFAULT '{}'::json NOT NULL,
    boundary_key character varying(256),
    schedule_occurrence_id character varying(36),
    CONSTRAINT ck_channel_outbox_origin CHECK ((((inbox_id IS NOT NULL) AND (schedule_occurrence_id IS NULL)) OR ((inbox_id IS NULL) AND (schedule_occurrence_id IS NOT NULL)))),
    CONSTRAINT ck_channel_outbox_state CHECK ((((status)::text = ANY ((ARRAY['pending'::character varying, 'sending'::character varying, 'sent'::character varying, 'failed'::character varying, 'unknown'::character varying, 'cancelled'::character varying])::text[])) AND (attempts >= 0)))
)""",

    """CREATE TABLE command_deliveries (
    command_id character varying(128) NOT NULL,
    delivery_generation bigint NOT NULL,
    retry_origin_generation bigint DEFAULT '1'::bigint NOT NULL,
    storage_node_id character varying(128) NOT NULL,
    target_incarnation character varying(128),
    channel character varying(16) NOT NULL,
    status character varying(24) DEFAULT 'pending'::character varying NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    published_at timestamp with time zone,
    lease_token character varying(128),
    lease_until timestamp with time zone,
    stream_message_id character varying(128),
    error_code character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_command_delivery_channel CHECK (((channel)::text = ANY ((ARRAY['execute'::character varying, 'control'::character varying])::text[]))),
    CONSTRAINT ck_command_delivery_lease CHECK (((((status)::text = 'publishing'::text) AND (lease_token IS NOT NULL) AND (lease_until IS NOT NULL)) OR (((status)::text <> 'publishing'::text) AND (lease_token IS NULL) AND (lease_until IS NULL)))),
    CONSTRAINT ck_command_delivery_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'publishing'::character varying, 'published'::character varying, 'superseded'::character varying, 'settled'::character varying, 'quarantined'::character varying])::text[]))),
    CONSTRAINT ck_command_delivery_versions CHECK (((delivery_generation > 0) AND (attempt_count >= 0) AND (retry_origin_generation > 0) AND (retry_origin_generation <= delivery_generation)))
)""",

    """CREATE TABLE command_delivery_quarantines (
    stream_key character varying(512) NOT NULL,
    message_id character varying(128) NOT NULL,
    command_id character varying(128),
    error_code character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE conversation_event_logs (
    conversation_id character varying(36) NOT NULL,
    generation character varying(36) NOT NULL,
    version bigint NOT NULL,
    status character varying(24) NOT NULL,
    initialization_token character varying(36) NOT NULL,
    lease_until timestamp with time zone,
    incarnation character varying(36),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_event_log_activation CHECK (((((status)::text = 'active'::text) AND (incarnation IS NOT NULL) AND (lease_until IS NULL)) OR (((status)::text = 'initializing'::text) AND (incarnation IS NULL) AND (lease_until IS NOT NULL)))),
    CONSTRAINT ck_event_log_status CHECK (((status)::text = ANY ((ARRAY['initializing'::character varying, 'active'::character varying])::text[]))),
    CONSTRAINT ck_event_log_version CHECK ((version > 0))
)""",

    """CREATE TABLE conversation_retirements (
    conversation_id character varying(36) NOT NULL,
    session_id character varying(128),
    storage_node_id character varying(128),
    execution_epoch bigint DEFAULT '0'::bigint NOT NULL,
    requested_by character varying(36) NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    completed_by character varying(128),
    purged_at timestamp with time zone,
    purged_by character varying(128),
    CONSTRAINT ck_retirement_epoch CHECK ((execution_epoch >= 0)),
    CONSTRAINT ck_retirement_storage CHECK (((session_id IS NULL) = (storage_node_id IS NULL)))
)""",

    """CREATE TABLE conversations (
    id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    agent_id character varying(36) NOT NULL,
    title character varying(255) NOT NULL,
    agentscope_session_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    model_profile character varying(64),
    model_profile_applied character varying(64),
    applied_credential_fingerprint character varying(64),
    active_product_task_id character varying(36),
    product_seq bigint DEFAULT '0'::bigint NOT NULL,
    deleted_at timestamp with time zone,
    business_seq bigint DEFAULT '0'::bigint NOT NULL,
    CONSTRAINT ck_conversation_business_seq CHECK (((business_seq >= 0) AND (business_seq <= '9007199254740991'::bigint))),
    CONSTRAINT ck_conversation_product_seq CHECK ((product_seq >= 0))
)""",

    """CREATE TABLE knowledge_bases (
    id character varying(36) NOT NULL,
    scope_id character varying(36) NOT NULL,
    name character varying(100) NOT NULL,
    description text NOT NULL,
    runtime_id character varying(64) NOT NULL,
    runtime_owner_id character varying(100) NOT NULL,
    enabled boolean NOT NULL,
    created_by character varying(36),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
)""",

    """CREATE TABLE knowledge_document_uploads (
    id character varying(36) NOT NULL,
    knowledge_base_id character varying(36) NOT NULL,
    filename character varying(255) NOT NULL,
    content_type character varying(150) NOT NULL,
    size integer NOT NULL,
    state character varying(20) NOT NULL,
    created_by character varying(36),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
)""",

    """CREATE TABLE mcp_secret_revisions (
    id character varying(36) NOT NULL,
    capability_id character varying(36) NOT NULL,
    scope_id character varying(36) NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE model_credentials (
    id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    profile_name character varying(64) NOT NULL,
    credential_id character varying(64) NOT NULL,
    config_fingerprint character varying(64),
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE permissions (
    id character varying(36) NOT NULL,
    key character varying(64) NOT NULL,
    description character varying(500) DEFAULT ''::character varying NOT NULL,
    display_name character varying(100) DEFAULT ''::character varying NOT NULL,
    is_system boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE product_events (
    id character varying(128) NOT NULL,
    conversation_id character varying(36) NOT NULL,
    product_seq bigint NOT NULL,
    task_id character varying(36),
    reply_id character varying(128),
    source_runtime_event_id character varying(128),
    projection_item_key character varying(128) NOT NULL,
    business_operation_key character varying(128),
    event_type character varying(128) NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    payload jsonb NOT NULL,
    business_seq bigint,
    CONSTRAINT ck_product_event_business_seq CHECK (((business_seq IS NULL) OR ((business_seq > 0) AND (business_seq <= '9007199254740991'::bigint)))),
    CONSTRAINT ck_product_event_seq CHECK ((product_seq > 0))
)""",

    """CREATE TABLE realtime_business_outbox (
    event_id character varying(128) NOT NULL,
    conversation_id character varying(36) NOT NULL,
    business_seq bigint NOT NULL,
    status character varying(24) DEFAULT 'pending'::character varying NOT NULL,
    claim_token character varying(36),
    attempt_count integer DEFAULT 0 NOT NULL,
    lease_until timestamp with time zone,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    published_generation character varying(36),
    published_incarnation character varying(36),
    published_stream_id character varying(41),
    error_code character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    CONSTRAINT ck_realtime_outbox_claim CHECK (((((status)::text = 'publishing'::text) AND (claim_token IS NOT NULL) AND (lease_until IS NOT NULL)) OR (((status)::text <> 'publishing'::text) AND (claim_token IS NULL) AND (lease_until IS NULL)))),
    CONSTRAINT ck_realtime_outbox_receipt CHECK ((((status)::text <> 'published'::text) OR ((published_generation IS NOT NULL) AND (published_incarnation IS NOT NULL) AND (published_stream_id IS NOT NULL) AND (published_at IS NOT NULL)))),
    CONSTRAINT ck_realtime_outbox_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'publishing'::character varying, 'published'::character varying, 'blocked'::character varying])::text[]))),
    CONSTRAINT ck_realtime_outbox_versions CHECK (((business_seq > 0) AND (business_seq <= '9007199254740991'::bigint) AND (attempt_count >= 0)))
)""",

    """CREATE TABLE recovery_forks (
    id character varying(36) NOT NULL,
    conversation_id character varying(36) NOT NULL,
    source_conversation_id character varying(36) NOT NULL,
    source_session_id character varying(128) NOT NULL,
    target_session_id character varying(128) NOT NULL,
    context_text text NOT NULL,
    source_task_id character varying(36) NOT NULL,
    target_task_id character varying(36) NOT NULL,
    target_node_id character varying(128) NOT NULL,
    actor_user_id character varying(36) NOT NULL,
    request_key character varying(36) NOT NULL,
    checkpoint_id character varying(36),
    manifest jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE resource_shares (
    id character varying(36) NOT NULL,
    agent_id character varying(36),
    capability_id character varying(36),
    target_scope_id character varying(36) NOT NULL,
    created_by character varying(36),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    include_descendants boolean DEFAULT false NOT NULL,
    knowledge_base_id character varying(36),
    allow_download boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_resource_share_one_resource CHECK ((((
CASE
    WHEN (agent_id IS NOT NULL) THEN 1
    ELSE 0
END +
CASE
    WHEN (capability_id IS NOT NULL) THEN 1
    ELSE 0
END) +
CASE
    WHEN (knowledge_base_id IS NOT NULL) THEN 1
    ELSE 0
END) = 1))
)""",

    """CREATE TABLE role_bindings (
    user_id character varying(36) NOT NULL,
    role_id character varying(36) NOT NULL,
    scope_id character varying(36) NOT NULL,
    include_descendants boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    delegated_permission_keys json,
    granted_by character varying(36)
)""",

    """CREATE TABLE role_permissions (
    id character varying(36) NOT NULL,
    role_id character varying(36) NOT NULL,
    permission_id character varying(36) NOT NULL
)""",

    """CREATE TABLE roles (
    id character varying(36) NOT NULL,
    key character varying(64) NOT NULL,
    display_name character varying(100) NOT NULL,
    description character varying(500) DEFAULT ''::character varying NOT NULL,
    is_system boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    delegatable boolean DEFAULT false NOT NULL
)""",

    """CREATE TABLE runtime_commands (
    id character varying(128) NOT NULL,
    session_id character varying(128) NOT NULL,
    conversation_id character varying(36) NOT NULL,
    task_id character varying(36),
    kind character varying(32) NOT NULL,
    actor_user_id character varying(36) NOT NULL,
    claimed_by character varying(128),
    claimed_epoch bigint,
    claimed_at timestamp with time zone,
    expected_epoch bigint DEFAULT '0'::bigint NOT NULL,
    status character varying(32) DEFAULT 'accepted'::character varying NOT NULL,
    payload jsonb NOT NULL,
    protocol_version integer DEFAULT 1 NOT NULL,
    content_digest character varying(64) NOT NULL,
    reply_id character varying(128),
    interaction_id character varying(128),
    request_version character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_runtime_claimed_epoch CHECK (((claimed_epoch IS NULL) OR (claimed_epoch > 0))),
    CONSTRAINT ck_runtime_command_digest CHECK ((length((content_digest)::text) = 64)),
    CONSTRAINT ck_runtime_command_kind CHECK (((kind)::text = ANY ((ARRAY['PrepareSession'::character varying, 'StartTask'::character varying, 'ResumeInteraction'::character varying, 'StopTask'::character varying])::text[]))),
    CONSTRAINT ck_runtime_command_status CHECK (((status)::text = ANY ((ARRAY['accepted'::character varying, 'claimed'::character varying, 'completed'::character varying, 'rejected'::character varying, 'indeterminate'::character varying])::text[]))),
    CONSTRAINT ck_runtime_command_versions CHECK (((expected_epoch >= 0) AND (protocol_version >= 1))),
    CONSTRAINT ck_runtime_start_task_identity CHECK ((((kind)::text <> 'StartTask'::text) OR (task_id IS NOT NULL)))
)""",

    """CREATE TABLE runtime_delivery_baselines (
    task_id character varying(36) NOT NULL,
    session_id character varying(128) NOT NULL,
    execution_epoch bigint NOT NULL,
    worker_id character varying(128) NOT NULL,
    storage_node_id character varying(128) NOT NULL,
    payload jsonb NOT NULL,
    digest character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_runtime_delivery_baselines_digest CHECK ((length((digest)::text) = 64)),
    CONSTRAINT ck_runtime_delivery_baselines_epoch CHECK ((execution_epoch > 0))
)""",

    """CREATE TABLE runtime_delivery_captures (
    task_id character varying(36) NOT NULL,
    session_id character varying(128) NOT NULL,
    execution_epoch bigint NOT NULL,
    worker_id character varying(128) NOT NULL,
    storage_node_id character varying(128) NOT NULL,
    payload jsonb NOT NULL,
    digest character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    snapshot_id character varying(128) NOT NULL,
    CONSTRAINT ck_runtime_delivery_captures_digest CHECK ((length((digest)::text) = 64)),
    CONSTRAINT ck_runtime_delivery_captures_epoch CHECK ((execution_epoch > 0))
)""",

    """CREATE TABLE runtime_delivery_seals (
    task_id character varying(36) NOT NULL,
    session_id character varying(128) NOT NULL,
    execution_epoch bigint NOT NULL,
    worker_id character varying(128) NOT NULL,
    storage_node_id character varying(128) NOT NULL,
    payload jsonb NOT NULL,
    digest character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_runtime_delivery_seals_digest CHECK ((length((digest)::text) = 64)),
    CONSTRAINT ck_runtime_delivery_seals_epoch CHECK ((execution_epoch > 0))
)""",

    """CREATE TABLE runtime_events (
    id character varying(128) NOT NULL,
    session_id character varying(128) NOT NULL,
    runtime_seq bigint NOT NULL,
    execution_epoch bigint NOT NULL,
    task_id character varying(36) NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_runtime_event_versions CHECK (((runtime_seq > 0) AND (execution_epoch > 0)))
)""",

    """CREATE TABLE runtime_execution_specs (
    task_id character varying(36) NOT NULL,
    session_id character varying(128) NOT NULL,
    schema_version integer NOT NULL,
    payload jsonb NOT NULL,
    digest character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_runtime_execution_spec_digest CHECK ((length((digest)::text) = 64)),
    CONSTRAINT ck_runtime_execution_spec_version CHECK ((schema_version = 1))
)""",

    """CREATE TABLE runtime_external_receipts (
    receipt_id character varying(128) NOT NULL,
    task_id character varying(36) NOT NULL,
    session_id character varying(128) NOT NULL,
    reply_id character varying(128) NOT NULL,
    execution_epoch bigint NOT NULL,
    tool_call_id character varying(128) NOT NULL,
    producer_id character varying(128) NOT NULL,
    job_id character varying(128) NOT NULL,
    payload jsonb NOT NULL,
    digest character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_external_receipt_digest CHECK ((length((digest)::text) = 64)),
    CONSTRAINT ck_external_receipt_epoch CHECK ((execution_epoch > 0))
)""",

    """CREATE TABLE runtime_memory_namespaces (
    user_id character varying(36) NOT NULL,
    agent_id character varying(36) NOT NULL,
    storage_node_id character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE runtime_node_quiescence_evidence (
    evidence_id character varying(128) NOT NULL,
    task_id character varying(36) NOT NULL,
    execution_epoch bigint NOT NULL,
    payload jsonb NOT NULL,
    digest character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
)""",

    """CREATE TABLE runtime_permission_upgrades (
    task_id character varying(36) NOT NULL,
    session_id character varying(128) NOT NULL,
    command_id character varying(128) NOT NULL,
    permission_version character varying(128) NOT NULL,
    state_version bigint NOT NULL,
    actor_user_id character varying(36) NOT NULL,
    worker_id character varying(128) NOT NULL,
    execution_epoch bigint NOT NULL,
    initial_profile character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_runtime_permission_upgrade_epoch CHECK ((execution_epoch > 0)),
    CONSTRAINT ck_runtime_permission_upgrade_profile CHECK (((initial_profile)::text = ANY ((ARRAY['standard'::character varying, 'strict'::character varying, 'read_only'::character varying, 'full_access'::character varying])::text[]))),
    CONSTRAINT ck_runtime_permission_upgrade_state CHECK ((state_version > 0))
)""",

    """CREATE TABLE runtime_projection_cursors (
    conversation_id character varying(36) NOT NULL,
    session_id character varying(128) NOT NULL,
    last_runtime_seq bigint DEFAULT '0'::bigint NOT NULL,
    version bigint DEFAULT '0'::bigint NOT NULL,
    CONSTRAINT ck_runtime_projection_cursor_versions CHECK (((last_runtime_seq >= 0) AND (version >= 0)))
)""",

    """CREATE TABLE runtime_resource_launch_resolutions (
    task_id character varying(36) NOT NULL,
    execution_epoch bigint NOT NULL,
    registration_seq bigint NOT NULL,
    payload jsonb NOT NULL,
    digest character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
)""",

    """CREATE TABLE runtime_resource_ledgers (
    task_id character varying(36) NOT NULL,
    execution_epoch bigint NOT NULL,
    worker_id character varying(128) NOT NULL,
    session_id character varying(128) NOT NULL,
    workspace jsonb NOT NULL,
    workspace_digest character varying(64) NOT NULL,
    resource_seq bigint DEFAULT '0'::bigint NOT NULL,
    closed boolean DEFAULT false NOT NULL,
    boundary jsonb,
    boundary_digest character varying(64),
    ledger_digest character varying(64),
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT ck_resource_ledger_closure CHECK ((((NOT closed) AND (boundary IS NULL) AND (boundary_digest IS NULL) AND (ledger_digest IS NULL)) OR (closed AND (resource_seq > 0) AND (boundary IS NOT NULL) AND (boundary_digest IS NOT NULL) AND (ledger_digest IS NOT NULL)))),
    CONSTRAINT ck_resource_ledger_versions CHECK (((execution_epoch > 0) AND (resource_seq >= 0)))
)""",

    """CREATE TABLE runtime_resource_registrations (
    task_id character varying(36) NOT NULL,
    execution_epoch bigint NOT NULL,
    registration_seq bigint NOT NULL,
    resource_id character varying(128) NOT NULL,
    payload jsonb NOT NULL,
    digest character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT ck_resource_registration_sequence CHECK ((registration_seq > 0))
)""",

    """CREATE TABLE runtime_sandbox_attestations (
    task_id character varying(36) NOT NULL,
    session_id character varying(128) NOT NULL,
    worker_id character varying(128) NOT NULL,
    execution_epoch bigint NOT NULL,
    workspace_id character varying(128) NOT NULL,
    isolation character varying(32) DEFAULT 'bubblewrap'::character varying NOT NULL,
    containment character varying(32) DEFAULT 'pid_namespace'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    instance_id character varying(256) DEFAULT ''::character varying NOT NULL,
    evidence json DEFAULT '{}'::json NOT NULL,
    CONSTRAINT ck_runtime_sandbox_epoch CHECK ((execution_epoch > 0)),
    CONSTRAINT ck_runtime_sandbox_proof_kind CHECK (((((isolation)::text = 'bubblewrap'::text) AND ((containment)::text = 'pid_namespace'::text)) OR (((isolation)::text = ANY ((ARRAY['docker'::character varying, 'kubernetes'::character varying, 'e2b'::character varying, 'daytona'::character varying, 'opensandbox'::character varying])::text[])) AND ((containment)::text = 'dedicated_uid'::text) AND (length(TRIM(BOTH FROM instance_id)) > 0))))
)""",

    """CREATE TABLE runtime_sessions (
    id character varying(128) NOT NULL,
    conversation_id character varying(36) NOT NULL,
    storage_node_id character varying(128) NOT NULL,
    active_task_id character varying(36),
    owner_id character varying(128),
    execution_epoch bigint DEFAULT '0'::bigint NOT NULL,
    lease_until timestamp with time zone,
    state_version bigint DEFAULT '0'::bigint NOT NULL,
    runtime_seq bigint DEFAULT '0'::bigint NOT NULL,
    status character varying(32) DEFAULT 'accepted'::character varying NOT NULL,
    snapshot jsonb,
    reply_id character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    snapshot_id character varying(128),
    superseded_at timestamp with time zone,
    CONSTRAINT ck_runtime_session_status CHECK (((status)::text = ANY ((ARRAY['accepted'::character varying, 'preparing'::character varying, 'running'::character varying, 'awaiting_interaction'::character varying, 'stopping'::character varying, 'completed'::character varying, 'failed'::character varying, 'cancelled'::character varying, 'interrupted'::character varying])::text[]))),
    CONSTRAINT ck_runtime_session_versions CHECK (((execution_epoch >= 0) AND (state_version >= 0) AND (runtime_seq >= 0)))
)""",

    """CREATE TABLE runtime_snapshots (
    id character varying(128) NOT NULL,
    session_id character varying(128) NOT NULL,
    task_id character varying(36) NOT NULL,
    state_version bigint NOT NULL,
    runtime_seq bigint NOT NULL,
    execution_epoch bigint NOT NULL,
    status character varying(32) NOT NULL,
    reply_id character varying(128),
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_runtime_snapshot_status CHECK (((status)::text = ANY ((ARRAY['awaiting_interaction'::character varying, 'completed'::character varying, 'failed'::character varying, 'cancelled'::character varying, 'interrupted'::character varying])::text[]))),
    CONSTRAINT ck_runtime_snapshot_versions CHECK (((state_version > 0) AND (runtime_seq > 0) AND (execution_epoch > 0)))
)""",

    """CREATE TABLE runtime_suspensions (
    snapshot_id character varying(128) NOT NULL,
    session_id character varying(128) NOT NULL,
    task_id character varying(36) NOT NULL,
    reply_id character varying(128) NOT NULL,
    storage_node_id character varying(128) NOT NULL,
    source_worker_id character varying(128) NOT NULL,
    source_epoch bigint NOT NULL,
    state_version bigint NOT NULL,
    runtime_seq bigint NOT NULL,
    resource_generation character varying(128) NOT NULL,
    snapshot_digest character varying(64) NOT NULL,
    status character varying(32) NOT NULL,
    cleanup_receipt jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    released_at timestamp with time zone,
    resume_worker_id character varying(128),
    resume_epoch bigint,
    resume_command_id character varying(128),
    CONSTRAINT ck_runtime_suspension_claim CHECK ((((resume_worker_id IS NULL) AND (resume_epoch IS NULL) AND (resume_command_id IS NULL)) OR (((status)::text = 'released'::text) AND (resume_worker_id IS NOT NULL) AND (resume_command_id IS NOT NULL) AND (resume_epoch = (source_epoch + 1))))),
    CONSTRAINT ck_runtime_suspension_proof CHECK (((length((snapshot_digest)::text) = 64) AND (length((resource_generation)::text) > 0))),
    CONSTRAINT ck_runtime_suspension_release CHECK (((((status)::text = 'released'::text) AND (cleanup_receipt IS NOT NULL) AND (released_at IS NOT NULL)) OR (((status)::text = ANY ((ARRAY['releasing'::character varying, 'blocked'::character varying])::text[])) AND (cleanup_receipt IS NULL) AND (released_at IS NULL)))),
    CONSTRAINT ck_runtime_suspension_status CHECK (((status)::text = ANY ((ARRAY['releasing'::character varying, 'released'::character varying, 'blocked'::character varying])::text[]))),
    CONSTRAINT ck_runtime_suspension_versions CHECK (((source_epoch > 0) AND (state_version > 0) AND (runtime_seq > 0)))
)""",

    """CREATE TABLE runtime_task_executions (
    task_id character varying(36) NOT NULL,
    session_id character varying(128) NOT NULL,
    status character varying(32) DEFAULT 'accepted'::character varying NOT NULL,
    execution_epoch bigint DEFAULT '0'::bigint NOT NULL,
    state_version bigint DEFAULT '0'::bigint NOT NULL,
    reply_id character varying(128),
    snapshot_id character varying(128),
    input_snapshot_id character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_runtime_execution_status CHECK (((status)::text = ANY ((ARRAY['accepted'::character varying, 'preparing'::character varying, 'running'::character varying, 'awaiting_interaction'::character varying, 'stopping'::character varying, 'completed'::character varying, 'failed'::character varying, 'cancelled'::character varying, 'interrupted'::character varying])::text[]))),
    CONSTRAINT ck_runtime_execution_versions CHECK (((execution_epoch >= 0) AND (state_version >= 0)))
)""",

    """CREATE TABLE runtime_worker_incarnations (
    worker_id character varying(128) NOT NULL,
    payload jsonb NOT NULL,
    digest character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL
)""",

    """CREATE TABLE schedule_occurrences (
    id character varying(36) NOT NULL,
    schedule_id character varying(36) NOT NULL,
    schedule_version integer NOT NULL,
    scheduled_for timestamp with time zone NOT NULL,
    triggered_at timestamp with time zone NOT NULL,
    outcome character varying(24) NOT NULL,
    reason text,
    task_id character varying(36),
    conversation_id character varying(36),
    snapshot json NOT NULL,
    missed_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_schedule_occurrence_outcome CHECK ((((outcome)::text = ANY ((ARRAY['task_created'::character varying, 'skipped_busy'::character varying, 'missed'::character varying, 'blocked'::character varying])::text[])) AND (missed_count >= 0)))
)""",

    """CREATE TABLE schedules (
    id character varying(36) NOT NULL,
    owner_user_id character varying(36) NOT NULL,
    agent_id character varying(36) NOT NULL,
    name character varying(80) NOT NULL,
    prompt text NOT NULL,
    model_profile character varying(64),
    timezone character varying(64) NOT NULL,
    rule json NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    next_fire_at timestamp with time zone,
    pause_reason text,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    notification json,
    CONSTRAINT ck_schedule_state CHECK ((((status)::text = ANY ((ARRAY['active'::character varying, 'paused'::character varying, 'completed'::character varying])::text[])) AND (version > 0)))
)""",

    """CREATE TABLE scope_closure (
    ancestor_id character varying(36) NOT NULL,
    descendant_id character varying(36) NOT NULL,
    depth integer NOT NULL
)""",

    """CREATE TABLE scope_memberships (
    scope_id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE scopes (
    id character varying(36) NOT NULL,
    kind character varying(16) NOT NULL,
    parent_id character varying(36),
    owner_user_id character varying(36),
    name character varying(200) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    normalized_name character varying(200) DEFAULT ''::character varying NOT NULL,
    tree_version integer DEFAULT 1 NOT NULL
)""",

    """CREATE TABLE security_audit_events (
    id character varying(36) NOT NULL,
    event_type character varying(64) NOT NULL,
    actor_user_id character varying(36),
    target_user_id character varying(36),
    scope_id character varying(36),
    request_id character varying(64),
    metadata json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE skill_artifacts (
    digest character varying(64) NOT NULL,
    format_version integer NOT NULL,
    manifest jsonb NOT NULL,
    name character varying(128) NOT NULL,
    description text NOT NULL,
    version text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    storage_key text NOT NULL,
    size_bytes bigint NOT NULL,
    CONSTRAINT ck_skill_artifact_digest CHECK ((length((digest)::text) = 64)),
    CONSTRAINT ck_skill_artifact_format CHECK ((format_version = 1)),
    CONSTRAINT ck_skill_artifact_location CHECK ((storage_key = (((('sha256/'::text || substr((digest)::text, 1, 2)) || '/'::text) || (digest)::text) || '.zip'::text))),
    CONSTRAINT ck_skill_artifact_size CHECK (((size_bytes > 0) AND (size_bytes <= 20971520)))
)""",

    """CREATE TABLE skill_upload_sources (
    scope_id character varying(36) NOT NULL,
    card_id character varying(64) NOT NULL,
    artifact_digest character varying(64) NOT NULL,
    uploaded_by character varying(36) NOT NULL,
    card jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE task_artifact_deliveries (
    task_id character varying(36) NOT NULL,
    artifact_id character varying(36) NOT NULL,
    change_type character varying(16) NOT NULL,
    metadata json NOT NULL,
    delivered_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE TABLE task_events (
    id integer NOT NULL,
    task_id character varying(36) NOT NULL,
    conversation_id character varying(36) NOT NULL,
    seq integer NOT NULL,
    event_type character varying(32) NOT NULL,
    payload json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)""",

    """CREATE SEQUENCE task_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1""",

    """ALTER SEQUENCE task_events_id_seq OWNED BY task_events.id""",

    """CREATE TABLE task_messages (
    id character varying(36) NOT NULL,
    task_id character varying(36) NOT NULL,
    conversation_id character varying(36) NOT NULL,
    role character varying(16) NOT NULL,
    content json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    snapshot_version bigint DEFAULT '0'::bigint NOT NULL,
    snapshot_reply_id character varying(128),
    snapshot_execution_epoch bigint,
    covered_cursor text,
    snapshot_business_seq bigint DEFAULT '0'::bigint NOT NULL,
    snapshot_phase character varying(24),
    snapshot_incomplete boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_message_snapshot_identity CHECK (((snapshot_version = 0) OR (((role)::text = 'assistant'::text) AND (snapshot_reply_id IS NOT NULL) AND (snapshot_execution_epoch IS NOT NULL) AND (snapshot_execution_epoch > 0) AND (snapshot_phase IS NOT NULL) AND ((snapshot_phase)::text = ANY ((ARRAY['partial'::character varying, 'approval'::character varying, 'final'::character varying, 'interrupted'::character varying])::text[]))))),
    CONSTRAINT ck_message_snapshot_versions CHECK (((snapshot_version >= 0) AND (snapshot_business_seq >= 0)))
)""",

    """CREATE TABLE task_projection_states (
    task_id character varying(36) NOT NULL,
    session_id character varying(128) NOT NULL,
    reply_id character varying(128),
    format_version integer DEFAULT 1 NOT NULL,
    state jsonb DEFAULT '{}'::jsonb NOT NULL,
    last_runtime_seq bigint DEFAULT '0'::bigint NOT NULL,
    CONSTRAINT ck_task_projection_format CHECK ((format_version = 1)),
    CONSTRAINT ck_task_projection_sequence CHECK ((last_runtime_seq >= 0))
)""",

    """CREATE TABLE task_usage_scopes (
    task_id character varying(36) NOT NULL,
    scope_id character varying(36) NOT NULL
)""",

    """CREATE TABLE tasks (
    id character varying(36) NOT NULL,
    conversation_id character varying(36) NOT NULL,
    title character varying(255) NOT NULL,
    status character varying(32) DEFAULT 'created'::character varying NOT NULL,
    run_id character varying(64),
    error json,
    duration_ms integer,
    steps integer,
    model_name character varying(128),
    token_usage json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    approval_profile character varying(16) DEFAULT 'standard'::character varying NOT NULL,
    last_decision_version character varying(128),
    artifact_output_snapshot json,
    artifact_presentation_snapshot character varying(64),
    artifact_baseline_captured boolean DEFAULT false NOT NULL,
    usage_model_profile character varying(64),
    usage_model_id character varying(255),
    usage_pricing json,
    usage_currency character varying(3),
    usage_cost numeric(28,10),
    origin character varying(16) DEFAULT 'user'::character varying NOT NULL,
    schedule_occurrence_id character varying(36),
    channel_inbox_id character varying(36),
    CONSTRAINT ck_task_schedule_origin CHECK (((((origin)::text = 'user'::text) AND (schedule_occurrence_id IS NULL) AND (channel_inbox_id IS NULL)) OR (((origin)::text = 'schedule'::text) AND (schedule_occurrence_id IS NOT NULL) AND (channel_inbox_id IS NULL) AND ((approval_profile)::text = 'unattended'::text)) OR (((origin)::text = 'channel'::text) AND (channel_inbox_id IS NOT NULL) AND (schedule_occurrence_id IS NULL) AND ((approval_profile)::text = 'standard'::text))))
)""",

    """CREATE TABLE tool_execution_receipts (
    id character varying(36) NOT NULL,
    task_id character varying(36) NOT NULL,
    tool_call_id character varying(128) NOT NULL,
    tool_name character varying(64) NOT NULL,
    request_hash character varying(64) NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    response json,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
)""",

    """CREATE TABLE users (
    id character varying(36) NOT NULL,
    email character varying(255) NOT NULL,
    password_hash character varying(255) NOT NULL,
    display_name character varying(100) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    normalized_email character varying(255) DEFAULT ''::character varying NOT NULL,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    token_version integer DEFAULT 0 NOT NULL,
    must_change_password boolean DEFAULT false NOT NULL,
    last_login_at timestamp with time zone,
    disabled_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    disabled_by character varying(36),
    access_version integer DEFAULT 1 NOT NULL,
    profile_version integer DEFAULT 1 NOT NULL
)""",

    """CREATE TABLE workspace_checkpoints (
    id character varying(36) NOT NULL,
    conversation_id character varying(36) NOT NULL,
    task_id character varying(36) NOT NULL,
    session_id character varying(128) NOT NULL,
    execution_epoch bigint NOT NULL,
    storage_node_id character varying(128) NOT NULL,
    boundary character varying(16) NOT NULL,
    status character varying(16) NOT NULL,
    manifest jsonb,
    digest character varying(64),
    error_code character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_workspace_checkpoint_boundary CHECK (((boundary)::text = ANY ((ARRAY['terminal'::character varying, 'approval'::character varying])::text[]))),
    CONSTRAINT ck_workspace_checkpoint_result CHECK (((((status)::text = 'available'::text) AND (manifest IS NOT NULL) AND (digest IS NOT NULL) AND (error_code IS NULL)) OR (((status)::text = 'unavailable'::text) AND (manifest IS NULL) AND (digest IS NULL) AND (error_code IS NOT NULL))))
)""",

    """CREATE TABLE workspace_release_requests (
    id character varying(128) NOT NULL,
    session_id character varying(128) NOT NULL,
    storage_node_id character varying(128) NOT NULL,
    expected_worker_id character varying(128) NOT NULL,
    expected_hold_version bigint NOT NULL,
    workspace_generation character varying(128) NOT NULL,
    reason character varying(32) DEFAULT 'idle'::character varying NOT NULL,
    status character varying(24) DEFAULT 'pending'::character varying NOT NULL,
    cleanup_receipt jsonb,
    error_code character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT ck_workspace_release_reason CHECK (((reason)::text = ANY ((ARRAY['idle'::character varying, 'execution_cleanup'::character varying])::text[]))),
    CONSTRAINT ck_workspace_release_receipt CHECK (((((status)::text = 'released'::text) AND (cleanup_receipt IS NOT NULL) AND (completed_at IS NOT NULL)) OR (((status)::text <> 'released'::text) AND (cleanup_receipt IS NULL)))),
    CONSTRAINT ck_workspace_release_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'releasing'::character varying, 'released'::character varying, 'stale'::character varying, 'blocked'::character varying])::text[]))),
    CONSTRAINT ck_workspace_release_version CHECK ((expected_hold_version > 0))
)""",

    """CREATE TABLE workspace_residencies (
    session_id character varying(128) NOT NULL,
    storage_node_id character varying(128) NOT NULL,
    workspace_generation character varying(128) NOT NULL,
    holder_worker_id character varying(128) NOT NULL,
    hold_version bigint NOT NULL,
    status character varying(24) NOT NULL,
    release_request_id character varying(128),
    last_task_id character varying(36) NOT NULL,
    last_execution_epoch bigint NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_workspace_residency_release CHECK ((((status)::text = 'held'::text) OR (release_request_id IS NOT NULL))),
    CONSTRAINT ck_workspace_residency_status CHECK (((status)::text = ANY ((ARRAY['held'::character varying, 'releasing'::character varying, 'released'::character varying, 'blocked'::character varying])::text[]))),
    CONSTRAINT ck_workspace_residency_versions CHECK (((hold_version > 0) AND (last_execution_epoch > 0)))
)""",

    """ALTER TABLE ONLY task_events ALTER COLUMN id SET DEFAULT nextval('task_events_id_seq'::regclass)""",

    """ALTER TABLE ONLY agent_capability_bindings
    ADD CONSTRAINT agent_capability_bindings_pkey PRIMARY KEY (agent_id, capability_installation_id)""",

    """ALTER TABLE ONLY agent_defaults
    ADD CONSTRAINT agent_defaults_pkey PRIMARY KEY (scope_id)""",

    """ALTER TABLE ONLY agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY agents
    ADD CONSTRAINT agents_slug_key UNIQUE (slug)""",

    """ALTER TABLE ONLY artifacts
    ADD CONSTRAINT artifacts_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY capability_installations
    ADD CONSTRAINT capability_installations_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY capability_mcp_revisions
    ADD CONSTRAINT capability_mcp_revisions_pkey PRIMARY KEY (capability_id)""",

    """ALTER TABLE ONLY capability_skill_artifacts
    ADD CONSTRAINT capability_skill_artifacts_pkey PRIMARY KEY (capability_id)""",

    """ALTER TABLE ONLY channel_binding_codes
    ADD CONSTRAINT channel_binding_codes_pkey PRIMARY KEY (code_hash)""",

    """ALTER TABLE ONLY channel_connections
    ADD CONSTRAINT channel_connections_bot_key_key UNIQUE (bot_key)""",

    """ALTER TABLE ONLY channel_connections
    ADD CONSTRAINT channel_connections_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY channel_conversations
    ADD CONSTRAINT channel_conversations_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY channel_identities
    ADD CONSTRAINT channel_identities_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY channel_inbox
    ADD CONSTRAINT channel_inbox_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY channel_inbox
    ADD CONSTRAINT channel_inbox_task_id_key UNIQUE (task_id)""",

    """ALTER TABLE ONLY channel_outbox
    ADD CONSTRAINT channel_outbox_dedupe_key_key UNIQUE (dedupe_key)""",

    """ALTER TABLE ONLY channel_outbox
    ADD CONSTRAINT channel_outbox_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY command_deliveries
    ADD CONSTRAINT command_deliveries_pkey PRIMARY KEY (command_id, delivery_generation)""",

    """ALTER TABLE ONLY command_delivery_quarantines
    ADD CONSTRAINT command_delivery_quarantines_pkey PRIMARY KEY (stream_key, message_id)""",

    """ALTER TABLE ONLY conversation_event_logs
    ADD CONSTRAINT conversation_event_logs_pkey PRIMARY KEY (conversation_id)""",

    """ALTER TABLE ONLY conversation_retirements
    ADD CONSTRAINT conversation_retirements_pkey PRIMARY KEY (conversation_id)""",

    """ALTER TABLE ONLY conversation_retirements
    ADD CONSTRAINT conversation_retirements_session_id_key UNIQUE (session_id)""",

    """ALTER TABLE ONLY conversations
    ADD CONSTRAINT conversations_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY knowledge_bases
    ADD CONSTRAINT knowledge_bases_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY knowledge_bases
    ADD CONSTRAINT knowledge_bases_runtime_id_key UNIQUE (runtime_id)""",

    """ALTER TABLE ONLY knowledge_document_uploads
    ADD CONSTRAINT knowledge_document_uploads_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY mcp_secret_revisions
    ADD CONSTRAINT mcp_secret_revisions_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY model_credentials
    ADD CONSTRAINT model_credentials_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY permissions
    ADD CONSTRAINT permissions_key_key UNIQUE (key)""",

    """ALTER TABLE ONLY permissions
    ADD CONSTRAINT permissions_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY product_events
    ADD CONSTRAINT product_events_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY realtime_business_outbox
    ADD CONSTRAINT realtime_business_outbox_pkey PRIMARY KEY (event_id)""",

    """ALTER TABLE ONLY recovery_forks
    ADD CONSTRAINT recovery_forks_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY recovery_forks
    ADD CONSTRAINT recovery_forks_source_session_id_key UNIQUE (source_session_id)""",

    """ALTER TABLE ONLY recovery_forks
    ADD CONSTRAINT recovery_forks_target_session_id_key UNIQUE (target_session_id)""",

    """ALTER TABLE ONLY resource_shares
    ADD CONSTRAINT resource_shares_agent_id_target_scope_id_key UNIQUE (agent_id, target_scope_id)""",

    """ALTER TABLE ONLY resource_shares
    ADD CONSTRAINT resource_shares_capability_id_target_scope_id_key UNIQUE (capability_id, target_scope_id)""",

    """ALTER TABLE ONLY resource_shares
    ADD CONSTRAINT resource_shares_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY role_bindings
    ADD CONSTRAINT role_bindings_pkey PRIMARY KEY (user_id, role_id, scope_id)""",

    """ALTER TABLE ONLY role_permissions
    ADD CONSTRAINT role_permissions_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY role_permissions
    ADD CONSTRAINT role_permissions_role_id_permission_id_key UNIQUE (role_id, permission_id)""",

    """ALTER TABLE ONLY roles
    ADD CONSTRAINT roles_key_key UNIQUE (key)""",

    """ALTER TABLE ONLY roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY runtime_commands
    ADD CONSTRAINT runtime_commands_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY runtime_delivery_baselines
    ADD CONSTRAINT runtime_delivery_baselines_pkey PRIMARY KEY (task_id)""",

    """ALTER TABLE ONLY runtime_delivery_captures
    ADD CONSTRAINT runtime_delivery_captures_pkey PRIMARY KEY (task_id)""",

    """ALTER TABLE ONLY runtime_delivery_seals
    ADD CONSTRAINT runtime_delivery_seals_pkey PRIMARY KEY (task_id)""",

    """ALTER TABLE ONLY runtime_events
    ADD CONSTRAINT runtime_events_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY runtime_execution_specs
    ADD CONSTRAINT runtime_execution_specs_pkey PRIMARY KEY (task_id)""",

    """ALTER TABLE ONLY runtime_external_receipts
    ADD CONSTRAINT runtime_external_receipts_pkey PRIMARY KEY (receipt_id)""",

    """ALTER TABLE ONLY runtime_memory_namespaces
    ADD CONSTRAINT runtime_memory_namespaces_pkey PRIMARY KEY (user_id, agent_id)""",

    """ALTER TABLE ONLY runtime_node_quiescence_evidence
    ADD CONSTRAINT runtime_node_quiescence_evidence_pkey PRIMARY KEY (evidence_id)""",

    """ALTER TABLE ONLY runtime_permission_upgrades
    ADD CONSTRAINT runtime_permission_upgrades_pkey PRIMARY KEY (command_id)""",

    """ALTER TABLE ONLY runtime_projection_cursors
    ADD CONSTRAINT runtime_projection_cursors_pkey PRIMARY KEY (conversation_id)""",

    """ALTER TABLE ONLY runtime_projection_cursors
    ADD CONSTRAINT runtime_projection_cursors_session_id_key UNIQUE (session_id)""",

    """ALTER TABLE ONLY runtime_resource_launch_resolutions
    ADD CONSTRAINT runtime_resource_launch_resolutions_pkey PRIMARY KEY (task_id, execution_epoch, registration_seq)""",

    """ALTER TABLE ONLY runtime_resource_ledgers
    ADD CONSTRAINT runtime_resource_ledgers_pkey PRIMARY KEY (task_id, execution_epoch)""",

    """ALTER TABLE ONLY runtime_resource_registrations
    ADD CONSTRAINT runtime_resource_registrations_pkey PRIMARY KEY (task_id, execution_epoch, registration_seq)""",

    """ALTER TABLE ONLY runtime_sandbox_attestations
    ADD CONSTRAINT runtime_sandbox_attestations_pkey PRIMARY KEY (task_id, execution_epoch)""",

    """ALTER TABLE ONLY runtime_sessions
    ADD CONSTRAINT runtime_sessions_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY runtime_snapshots
    ADD CONSTRAINT runtime_snapshots_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY runtime_suspensions
    ADD CONSTRAINT runtime_suspensions_pkey PRIMARY KEY (snapshot_id)""",

    """ALTER TABLE ONLY runtime_task_executions
    ADD CONSTRAINT runtime_task_executions_pkey PRIMARY KEY (task_id)""",

    """ALTER TABLE ONLY runtime_worker_incarnations
    ADD CONSTRAINT runtime_worker_incarnations_pkey PRIMARY KEY (worker_id)""",

    """ALTER TABLE ONLY schedule_occurrences
    ADD CONSTRAINT schedule_occurrences_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY schedule_occurrences
    ADD CONSTRAINT schedule_occurrences_task_id_key UNIQUE (task_id)""",

    """ALTER TABLE ONLY schedules
    ADD CONSTRAINT schedules_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY scope_closure
    ADD CONSTRAINT scope_closure_pkey PRIMARY KEY (ancestor_id, descendant_id)""",

    """ALTER TABLE ONLY scope_memberships
    ADD CONSTRAINT scope_memberships_pkey PRIMARY KEY (scope_id, user_id)""",

    """ALTER TABLE ONLY scopes
    ADD CONSTRAINT scopes_owner_user_id_key UNIQUE (owner_user_id)""",

    """ALTER TABLE ONLY scopes
    ADD CONSTRAINT scopes_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY security_audit_events
    ADD CONSTRAINT security_audit_events_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY skill_artifacts
    ADD CONSTRAINT skill_artifacts_pkey PRIMARY KEY (digest)""",

    """ALTER TABLE ONLY skill_upload_sources
    ADD CONSTRAINT skill_upload_sources_pkey PRIMARY KEY (scope_id, card_id)""",

    """ALTER TABLE ONLY task_artifact_deliveries
    ADD CONSTRAINT task_artifact_deliveries_pkey PRIMARY KEY (task_id, artifact_id)""",

    """ALTER TABLE ONLY task_events
    ADD CONSTRAINT task_events_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY task_messages
    ADD CONSTRAINT task_messages_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY task_projection_states
    ADD CONSTRAINT task_projection_states_pkey PRIMARY KEY (task_id)""",

    """ALTER TABLE ONLY task_usage_scopes
    ADD CONSTRAINT task_usage_scopes_pkey PRIMARY KEY (task_id, scope_id)""",

    """ALTER TABLE ONLY tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY tool_execution_receipts
    ADD CONSTRAINT tool_execution_receipts_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY tool_execution_receipts
    ADD CONSTRAINT tool_execution_receipts_task_id_tool_call_id_key UNIQUE (task_id, tool_call_id)""",

    """ALTER TABLE ONLY agents
    ADD CONSTRAINT uq_agents_scope_system_key UNIQUE (scope_id, system_key)""",

    """ALTER TABLE ONLY channel_conversations
    ADD CONSTRAINT uq_channel_conversation UNIQUE (channel_id, user_id, agent_id, chat_key)""",

    """ALTER TABLE ONLY channel_identities
    ADD CONSTRAINT uq_channel_identity UNIQUE (channel_id, external_user_id)""",

    """ALTER TABLE ONLY channel_inbox
    ADD CONSTRAINT uq_channel_inbox_message UNIQUE (channel_id, message_id)""",

    """ALTER TABLE ONLY channel_outbox
    ADD CONSTRAINT uq_channel_outbox_schedule_occurrence UNIQUE (schedule_occurrence_id)""",

    """ALTER TABLE ONLY runtime_external_receipts
    ADD CONSTRAINT uq_external_receipt_job UNIQUE (producer_id, job_id)""",

    """ALTER TABLE ONLY runtime_external_receipts
    ADD CONSTRAINT uq_external_receipt_tool UNIQUE (task_id, tool_call_id)""",

    """ALTER TABLE ONLY model_credentials
    ADD CONSTRAINT uq_model_credentials_user_fingerprint UNIQUE (user_id, profile_name, config_fingerprint)""",

    """ALTER TABLE ONLY runtime_node_quiescence_evidence
    ADD CONSTRAINT uq_node_quiescence_execution UNIQUE (task_id, execution_epoch)""",

    """ALTER TABLE ONLY product_events
    ADD CONSTRAINT uq_product_events_business_item UNIQUE (conversation_id, business_operation_key, projection_item_key)""",

    """ALTER TABLE ONLY product_events
    ADD CONSTRAINT uq_product_events_business_seq UNIQUE (conversation_id, business_seq)""",

    """ALTER TABLE ONLY product_events
    ADD CONSTRAINT uq_product_events_conversation_seq UNIQUE (conversation_id, product_seq)""",

    """ALTER TABLE ONLY product_events
    ADD CONSTRAINT uq_product_events_runtime_item UNIQUE (source_runtime_event_id, projection_item_key)""",

    """ALTER TABLE ONLY realtime_business_outbox
    ADD CONSTRAINT uq_realtime_outbox_business_seq UNIQUE (conversation_id, business_seq)""",

    """ALTER TABLE ONLY recovery_forks
    ADD CONSTRAINT uq_recovery_fork_request UNIQUE (actor_user_id, request_key)""",

    """ALTER TABLE ONLY runtime_resource_registrations
    ADD CONSTRAINT uq_resource_registration_identity UNIQUE (task_id, execution_epoch, resource_id)""",

    """ALTER TABLE ONLY resource_shares
    ADD CONSTRAINT uq_resource_shares_knowledge_base_id_target_scope_id UNIQUE (knowledge_base_id, target_scope_id)""",

    """ALTER TABLE ONLY runtime_events
    ADD CONSTRAINT uq_runtime_events_session_seq UNIQUE (session_id, runtime_seq)""",

    """ALTER TABLE ONLY runtime_permission_upgrades
    ADD CONSTRAINT uq_runtime_permission_upgrade_boundary UNIQUE (task_id, state_version)""",

    """ALTER TABLE ONLY runtime_permission_upgrades
    ADD CONSTRAINT uq_runtime_permission_upgrade_version UNIQUE (task_id, permission_version)""",

    """ALTER TABLE ONLY runtime_snapshots
    ADD CONSTRAINT uq_runtime_snapshot_boundary UNIQUE (id, task_id, session_id, state_version)""",

    """ALTER TABLE ONLY runtime_snapshots
    ADD CONSTRAINT uq_runtime_snapshot_identity_session UNIQUE (id, session_id)""",

    """ALTER TABLE ONLY runtime_snapshots
    ADD CONSTRAINT uq_runtime_snapshot_session_version UNIQUE (session_id, state_version)""",

    """ALTER TABLE ONLY schedule_occurrences
    ADD CONSTRAINT uq_schedule_occurrence_due UNIQUE (schedule_id, scheduled_for)""",

    """ALTER TABLE ONLY tasks
    ADD CONSTRAINT uq_tasks_channel_inbox_id UNIQUE (channel_inbox_id)""",

    """ALTER TABLE ONLY tasks
    ADD CONSTRAINT uq_tasks_schedule_occurrence_id UNIQUE (schedule_occurrence_id)""",

    """ALTER TABLE ONLY workspace_release_requests
    ADD CONSTRAINT uq_workspace_release_hold UNIQUE (session_id, expected_hold_version)""",

    """ALTER TABLE ONLY users
    ADD CONSTRAINT users_email_key UNIQUE (email)""",

    """ALTER TABLE ONLY users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY workspace_checkpoints
    ADD CONSTRAINT workspace_checkpoints_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY workspace_release_requests
    ADD CONSTRAINT workspace_release_requests_pkey PRIMARY KEY (id)""",

    """ALTER TABLE ONLY workspace_residencies
    ADD CONSTRAINT workspace_residencies_pkey PRIMARY KEY (session_id)""",

    """CREATE UNIQUE INDEX ix_agents_scope_slug ON agents USING btree (scope_id, slug)""",

    """CREATE INDEX ix_agents_system_key ON agents USING btree (system_key)""",

    """CREATE INDEX ix_artifacts_conversation_id ON artifacts USING btree (conversation_id)""",

    """CREATE INDEX ix_artifacts_task_id ON artifacts USING btree (task_id)""",

    """CREATE INDEX ix_capability_installations_scope_id ON capability_installations USING btree (scope_id)""",

    """CREATE INDEX ix_channel_inbox_pending ON channel_inbox USING btree (status, created_at)""",

    """CREATE INDEX ix_channel_outbox_due ON channel_outbox USING btree (status, next_attempt_at)""",

    """CREATE INDEX ix_command_deliveries_due ON command_deliveries USING btree (status, next_attempt_at)""",

    """CREATE INDEX ix_conversations_deleted_at ON conversations USING btree (deleted_at)""",

    """CREATE INDEX ix_conversations_user_id ON conversations USING btree (user_id)""",

    """CREATE INDEX ix_knowledge_bases_scope_id ON knowledge_bases USING btree (scope_id)""",

    """CREATE INDEX ix_knowledge_document_uploads_knowledge_base_id ON knowledge_document_uploads USING btree (knowledge_base_id)""",

    """CREATE INDEX ix_model_credentials_user_id ON model_credentials USING btree (user_id)""",

    """CREATE INDEX ix_realtime_outbox_due ON realtime_business_outbox USING btree (status, next_attempt_at)""",

    """CREATE INDEX ix_resource_shares_agent_id ON resource_shares USING btree (agent_id)""",

    """CREATE INDEX ix_resource_shares_capability_id ON resource_shares USING btree (capability_id)""",

    """CREATE INDEX ix_resource_shares_knowledge_base_id ON resource_shares USING btree (knowledge_base_id)""",

    """CREATE INDEX ix_resource_shares_target_scope_id ON resource_shares USING btree (target_scope_id)""",

    """CREATE INDEX ix_retirement_pending_node ON conversation_retirements USING btree (storage_node_id, completed_at, conversation_id)""",

    """CREATE INDEX ix_retirement_purge_pending ON conversation_retirements USING btree (purged_at, storage_node_id, conversation_id)""",

    """CREATE INDEX ix_role_permissions_permission_id ON role_permissions USING btree (permission_id)""",

    """CREATE INDEX ix_role_permissions_role_id ON role_permissions USING btree (role_id)""",

    """CREATE INDEX ix_runtime_commands_session_status ON runtime_commands USING btree (session_id, status)""",

    """CREATE INDEX ix_runtime_executions_session ON runtime_task_executions USING btree (session_id)""",

    """CREATE INDEX ix_runtime_sessions_storage_status ON runtime_sessions USING btree (storage_node_id, status)""",

    """CREATE INDEX ix_runtime_suspension_node_status ON runtime_suspensions USING btree (storage_node_id, status)""",

    """CREATE INDEX ix_schedule_occurrences_history ON schedule_occurrences USING btree (schedule_id, scheduled_for)""",

    """CREATE INDEX ix_schedule_occurrences_version ON schedule_occurrences USING btree (schedule_id, schedule_version)""",

    """CREATE INDEX ix_schedules_due ON schedules USING btree (next_fire_at, id) WHERE (((status)::text = 'active'::text) AND (deleted_at IS NULL))""",

    """CREATE INDEX ix_schedules_owner_user_id ON schedules USING btree (owner_user_id)""",

    """CREATE INDEX ix_scope_memberships_user_id ON scope_memberships USING btree (user_id)""",

    """CREATE INDEX ix_scopes_parent_id ON scopes USING btree (parent_id)""",

    """CREATE INDEX ix_security_audit_events_event_type ON security_audit_events USING btree (event_type)""",

    """CREATE INDEX ix_task_artifact_deliveries_artifact_id ON task_artifact_deliveries USING btree (artifact_id)""",

    """CREATE INDEX ix_task_messages_conversation_id ON task_messages USING btree (conversation_id)""",

    """CREATE INDEX ix_task_messages_task_id ON task_messages USING btree (task_id)""",

    """CREATE INDEX ix_task_usage_scopes_scope_task ON task_usage_scopes USING btree (scope_id, task_id)""",

    """CREATE INDEX ix_tasks_conversation_id ON tasks USING btree (conversation_id)""",

    """CREATE INDEX ix_tasks_created_conversation ON tasks USING btree (created_at, conversation_id)""",

    """CREATE INDEX ix_tasks_model_unavailable ON tasks USING btree (id) WHERE ((error ->> 'code'::text) = 'model_unavailable'::text)""",

    """CREATE INDEX ix_tasks_status ON tasks USING btree (status)""",

    """CREATE INDEX ix_tool_execution_receipts_task_id ON tool_execution_receipts USING btree (task_id)""",

    """CREATE INDEX ix_users_email ON users USING btree (email)""",

    """CREATE INDEX ix_users_status ON users USING btree (status)""",

    """CREATE INDEX ix_workspace_checkpoint_conversation ON workspace_checkpoints USING btree (conversation_id, created_at)""",

    """CREATE INDEX ix_workspace_release_pending ON workspace_release_requests USING btree (storage_node_id, expected_worker_id, status)""",

    """CREATE INDEX ix_workspace_residency_holder ON workspace_residencies USING btree (storage_node_id, holder_worker_id, status)""",

    """CREATE UNIQUE INDEX uq_runtime_commands_start_task ON runtime_commands USING btree (task_id) WHERE ((kind)::text = 'StartTask'::text)""",

    """CREATE UNIQUE INDEX uq_runtime_sessions_current_conversation ON runtime_sessions USING btree (conversation_id) WHERE (superseded_at IS NULL)""",

    """CREATE UNIQUE INDEX uq_scopes_department_sibling_name ON scopes USING btree (parent_id, normalized_name) WHERE ((kind)::text = 'department'::text)""",

    """CREATE UNIQUE INDEX uq_task_events_task_seq ON task_events USING btree (task_id, seq)""",

    """CREATE UNIQUE INDEX uq_users_normalized_email ON users USING btree (normalized_email)""",

    """CREATE TRIGGER channel_inbox_immutable BEFORE DELETE OR UPDATE ON channel_inbox FOR EACH ROW EXECUTE FUNCTION reject_channel_inbox_mutation()""",

    """CREATE TRIGGER external_receipt_immutable BEFORE DELETE OR UPDATE ON runtime_external_receipts FOR EACH ROW EXECUTE FUNCTION reject_external_receipt_mutation()""",

    """CREATE TRIGGER mcp_secret_revision_immutable BEFORE DELETE OR UPDATE ON mcp_secret_revisions FOR EACH ROW EXECUTE FUNCTION reject_mcp_secret_revision_mutation()""",

    """CREATE TRIGGER protect_conversation_retirement BEFORE DELETE OR UPDATE ON conversation_retirements FOR EACH ROW EXECUTE FUNCTION protect_conversation_retirement()""",

    """CREATE TRIGGER protect_conversation_tombstone BEFORE UPDATE ON conversations FOR EACH ROW EXECUTE FUNCTION protect_conversation_tombstone()""",

    """CREATE TRIGGER runtime_delivery_baselines_immutable BEFORE DELETE OR UPDATE ON runtime_delivery_baselines FOR EACH ROW EXECUTE FUNCTION reject_runtime_delivery_baselines_mutation()""",

    """CREATE TRIGGER runtime_delivery_captures_immutable BEFORE DELETE OR UPDATE ON runtime_delivery_captures FOR EACH ROW EXECUTE FUNCTION reject_runtime_delivery_captures_mutation()""",

    """CREATE TRIGGER runtime_delivery_seals_immutable BEFORE DELETE OR UPDATE ON runtime_delivery_seals FOR EACH ROW EXECUTE FUNCTION reject_runtime_delivery_seals_mutation()""",

    """CREATE TRIGGER runtime_execution_spec_immutable BEFORE DELETE OR UPDATE ON runtime_execution_specs FOR EACH ROW EXECUTE FUNCTION reject_runtime_execution_spec_mutation()""",

    """CREATE TRIGGER runtime_node_quiescence_evidence_immutable BEFORE DELETE OR UPDATE ON runtime_node_quiescence_evidence FOR EACH ROW EXECUTE FUNCTION reject_runtime_node_quiescence_evidence_mutation()""",

    """CREATE TRIGGER runtime_permission_upgrades_immutable BEFORE DELETE OR UPDATE ON runtime_permission_upgrades FOR EACH ROW EXECUTE FUNCTION reject_runtime_permission_mutation()""",

    """CREATE TRIGGER runtime_resource_launch_resolutions_immutable BEFORE DELETE OR UPDATE ON runtime_resource_launch_resolutions FOR EACH ROW EXECUTE FUNCTION reject_runtime_resource_launch_resolutions_mutation()""",

    """CREATE TRIGGER runtime_resource_ledger_guard BEFORE DELETE OR UPDATE ON runtime_resource_ledgers FOR EACH ROW EXECUTE FUNCTION protect_runtime_resource_ledger()""",

    """CREATE TRIGGER runtime_resource_registrations_immutable BEFORE DELETE OR UPDATE ON runtime_resource_registrations FOR EACH ROW EXECUTE FUNCTION reject_runtime_resource_registrations_mutation()""",

    """CREATE TRIGGER runtime_sandbox_attestations_immutable BEFORE DELETE OR UPDATE ON runtime_sandbox_attestations FOR EACH ROW EXECUTE FUNCTION reject_runtime_permission_mutation()""",

    """CREATE TRIGGER runtime_snapshot_immutable BEFORE DELETE OR UPDATE ON runtime_snapshots FOR EACH ROW EXECUTE FUNCTION reject_runtime_snapshot_mutation()""",

    """CREATE TRIGGER runtime_worker_incarnations_immutable BEFORE DELETE OR UPDATE ON runtime_worker_incarnations FOR EACH ROW EXECUTE FUNCTION reject_runtime_worker_incarnations_mutation()""",

    """CREATE TRIGGER schedule_occurrences_immutable BEFORE DELETE OR UPDATE ON schedule_occurrences FOR EACH ROW EXECUTE FUNCTION reject_schedule_occurrence_mutation()""",

    """CREATE TRIGGER skill_artifact_immutable BEFORE DELETE OR UPDATE ON skill_artifacts FOR EACH ROW EXECUTE FUNCTION reject_skill_artifact_mutation()""",

    """ALTER TABLE ONLY agent_capability_bindings
    ADD CONSTRAINT agent_capability_bindings_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agents(id)""",

    """ALTER TABLE ONLY agent_capability_bindings
    ADD CONSTRAINT agent_capability_bindings_capability_installation_id_fkey FOREIGN KEY (capability_installation_id) REFERENCES capability_installations(id)""",

    """ALTER TABLE ONLY agent_defaults
    ADD CONSTRAINT agent_defaults_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agents(id)""",

    """ALTER TABLE ONLY agent_defaults
    ADD CONSTRAINT agent_defaults_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY agent_defaults
    ADD CONSTRAINT agent_defaults_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES users(id)""",

    """ALTER TABLE ONLY agents
    ADD CONSTRAINT agents_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id)""",

    """ALTER TABLE ONLY agents
    ADD CONSTRAINT agents_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY artifacts
    ADD CONSTRAINT artifacts_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY artifacts
    ADD CONSTRAINT artifacts_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY capability_installations
    ADD CONSTRAINT capability_installations_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id)""",

    """ALTER TABLE ONLY capability_installations
    ADD CONSTRAINT capability_installations_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY capability_mcp_revisions
    ADD CONSTRAINT capability_mcp_revisions_capability_id_fkey FOREIGN KEY (capability_id) REFERENCES capability_installations(id) ON DELETE CASCADE""",

    """ALTER TABLE ONLY capability_mcp_revisions
    ADD CONSTRAINT capability_mcp_revisions_secret_version_fkey FOREIGN KEY (secret_version) REFERENCES mcp_secret_revisions(id)""",

    """ALTER TABLE ONLY capability_skill_artifacts
    ADD CONSTRAINT capability_skill_artifacts_artifact_digest_fkey FOREIGN KEY (artifact_digest) REFERENCES skill_artifacts(digest)""",

    """ALTER TABLE ONLY capability_skill_artifacts
    ADD CONSTRAINT capability_skill_artifacts_capability_id_fkey FOREIGN KEY (capability_id) REFERENCES capability_installations(id) ON DELETE CASCADE""",

    """ALTER TABLE ONLY channel_binding_codes
    ADD CONSTRAINT channel_binding_codes_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES channel_connections(id)""",

    """ALTER TABLE ONLY channel_binding_codes
    ADD CONSTRAINT channel_binding_codes_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY channel_connections
    ADD CONSTRAINT channel_connections_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agents(id)""",

    """ALTER TABLE ONLY channel_connections
    ADD CONSTRAINT channel_connections_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY channel_connections
    ADD CONSTRAINT channel_connections_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY channel_conversations
    ADD CONSTRAINT channel_conversations_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agents(id)""",

    """ALTER TABLE ONLY channel_conversations
    ADD CONSTRAINT channel_conversations_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES channel_connections(id)""",

    """ALTER TABLE ONLY channel_conversations
    ADD CONSTRAINT channel_conversations_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY channel_conversations
    ADD CONSTRAINT channel_conversations_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY channel_identities
    ADD CONSTRAINT channel_identities_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES channel_connections(id)""",

    """ALTER TABLE ONLY channel_identities
    ADD CONSTRAINT channel_identities_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY channel_inbox
    ADD CONSTRAINT channel_inbox_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES channel_connections(id)""",

    """ALTER TABLE ONLY channel_inbox
    ADD CONSTRAINT channel_inbox_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY channel_inbox
    ADD CONSTRAINT channel_inbox_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY channel_inbox
    ADD CONSTRAINT channel_inbox_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY channel_outbox
    ADD CONSTRAINT channel_outbox_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES channel_connections(id)""",

    """ALTER TABLE ONLY channel_outbox
    ADD CONSTRAINT channel_outbox_inbox_id_fkey FOREIGN KEY (inbox_id) REFERENCES channel_inbox(id)""",

    """ALTER TABLE ONLY channel_outbox
    ADD CONSTRAINT channel_outbox_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY channel_outbox
    ADD CONSTRAINT channel_outbox_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY command_deliveries
    ADD CONSTRAINT command_deliveries_command_id_fkey FOREIGN KEY (command_id) REFERENCES runtime_commands(id)""",

    """ALTER TABLE ONLY conversation_event_logs
    ADD CONSTRAINT conversation_event_logs_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY conversation_retirements
    ADD CONSTRAINT conversation_retirements_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY conversation_retirements
    ADD CONSTRAINT conversation_retirements_requested_by_fkey FOREIGN KEY (requested_by) REFERENCES users(id)""",

    """ALTER TABLE ONLY conversation_retirements
    ADD CONSTRAINT conversation_retirements_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY conversations
    ADD CONSTRAINT conversations_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agents(id)""",

    """ALTER TABLE ONLY conversations
    ADD CONSTRAINT conversations_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY channel_outbox
    ADD CONSTRAINT fk_channel_outbox_schedule_occurrence FOREIGN KEY (schedule_occurrence_id) REFERENCES schedule_occurrences(id)""",

    """ALTER TABLE ONLY conversations
    ADD CONSTRAINT fk_conversation_active_product_task FOREIGN KEY (active_product_task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY resource_shares
    ADD CONSTRAINT fk_resource_shares_knowledge_base_id FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE""",

    """ALTER TABLE ONLY role_bindings
    ADD CONSTRAINT fk_role_bindings_granted_by FOREIGN KEY (granted_by) REFERENCES users(id)""",

    """ALTER TABLE ONLY runtime_task_executions
    ADD CONSTRAINT fk_runtime_execution_input_snapshot FOREIGN KEY (input_snapshot_id, session_id) REFERENCES runtime_snapshots(id, session_id)""",

    """ALTER TABLE ONLY runtime_task_executions
    ADD CONSTRAINT fk_runtime_execution_snapshot FOREIGN KEY (snapshot_id, task_id, session_id, state_version) REFERENCES runtime_snapshots(id, task_id, session_id, state_version)""",

    """ALTER TABLE ONLY runtime_sessions
    ADD CONSTRAINT fk_runtime_session_snapshot FOREIGN KEY (snapshot_id, id) REFERENCES runtime_snapshots(id, session_id)""",

    """ALTER TABLE ONLY runtime_suspensions
    ADD CONSTRAINT fk_runtime_suspension_boundary FOREIGN KEY (snapshot_id, task_id, session_id, state_version) REFERENCES runtime_snapshots(id, task_id, session_id, state_version)""",

    """ALTER TABLE ONLY tasks
    ADD CONSTRAINT fk_task_channel_inbox FOREIGN KEY (channel_inbox_id) REFERENCES channel_inbox(id)""",

    """ALTER TABLE ONLY tasks
    ADD CONSTRAINT fk_task_schedule_occurrence FOREIGN KEY (schedule_occurrence_id) REFERENCES schedule_occurrences(id)""",

    """ALTER TABLE ONLY knowledge_bases
    ADD CONSTRAINT knowledge_bases_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL""",

    """ALTER TABLE ONLY knowledge_bases
    ADD CONSTRAINT knowledge_bases_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY knowledge_document_uploads
    ADD CONSTRAINT knowledge_document_uploads_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL""",

    """ALTER TABLE ONLY knowledge_document_uploads
    ADD CONSTRAINT knowledge_document_uploads_knowledge_base_id_fkey FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id)""",

    """ALTER TABLE ONLY mcp_secret_revisions
    ADD CONSTRAINT mcp_secret_revisions_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY model_credentials
    ADD CONSTRAINT model_credentials_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY product_events
    ADD CONSTRAINT product_events_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY product_events
    ADD CONSTRAINT product_events_source_runtime_event_id_fkey FOREIGN KEY (source_runtime_event_id) REFERENCES runtime_events(id)""",

    """ALTER TABLE ONLY product_events
    ADD CONSTRAINT product_events_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY realtime_business_outbox
    ADD CONSTRAINT realtime_business_outbox_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY realtime_business_outbox
    ADD CONSTRAINT realtime_business_outbox_event_id_fkey FOREIGN KEY (event_id) REFERENCES product_events(id)""",

    """ALTER TABLE ONLY recovery_forks
    ADD CONSTRAINT recovery_forks_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY recovery_forks
    ADD CONSTRAINT recovery_forks_checkpoint_id_fkey FOREIGN KEY (checkpoint_id) REFERENCES workspace_checkpoints(id)""",

    """ALTER TABLE ONLY recovery_forks
    ADD CONSTRAINT recovery_forks_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY recovery_forks
    ADD CONSTRAINT recovery_forks_source_conversation_id_fkey FOREIGN KEY (source_conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY recovery_forks
    ADD CONSTRAINT recovery_forks_source_session_id_fkey FOREIGN KEY (source_session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY recovery_forks
    ADD CONSTRAINT recovery_forks_source_task_id_fkey FOREIGN KEY (source_task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY recovery_forks
    ADD CONSTRAINT recovery_forks_target_session_id_fkey FOREIGN KEY (target_session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY resource_shares
    ADD CONSTRAINT resource_shares_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE""",

    """ALTER TABLE ONLY resource_shares
    ADD CONSTRAINT resource_shares_capability_id_fkey FOREIGN KEY (capability_id) REFERENCES capability_installations(id) ON DELETE CASCADE""",

    """ALTER TABLE ONLY resource_shares
    ADD CONSTRAINT resource_shares_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL""",

    """ALTER TABLE ONLY resource_shares
    ADD CONSTRAINT resource_shares_target_scope_id_fkey FOREIGN KEY (target_scope_id) REFERENCES scopes(id) ON DELETE CASCADE""",

    """ALTER TABLE ONLY role_bindings
    ADD CONSTRAINT role_bindings_role_id_fkey FOREIGN KEY (role_id) REFERENCES roles(id)""",

    """ALTER TABLE ONLY role_bindings
    ADD CONSTRAINT role_bindings_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY role_bindings
    ADD CONSTRAINT role_bindings_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY role_permissions
    ADD CONSTRAINT role_permissions_permission_id_fkey FOREIGN KEY (permission_id) REFERENCES permissions(id)""",

    """ALTER TABLE ONLY role_permissions
    ADD CONSTRAINT role_permissions_role_id_fkey FOREIGN KEY (role_id) REFERENCES roles(id)""",

    """ALTER TABLE ONLY runtime_commands
    ADD CONSTRAINT runtime_commands_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY runtime_commands
    ADD CONSTRAINT runtime_commands_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY runtime_commands
    ADD CONSTRAINT runtime_commands_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_commands
    ADD CONSTRAINT runtime_commands_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_delivery_baselines
    ADD CONSTRAINT runtime_delivery_baselines_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_delivery_baselines
    ADD CONSTRAINT runtime_delivery_baselines_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_delivery_captures
    ADD CONSTRAINT runtime_delivery_captures_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_delivery_captures
    ADD CONSTRAINT runtime_delivery_captures_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES runtime_snapshots(id)""",

    """ALTER TABLE ONLY runtime_delivery_captures
    ADD CONSTRAINT runtime_delivery_captures_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_delivery_seals
    ADD CONSTRAINT runtime_delivery_seals_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_delivery_seals
    ADD CONSTRAINT runtime_delivery_seals_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_events
    ADD CONSTRAINT runtime_events_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_events
    ADD CONSTRAINT runtime_events_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_execution_specs
    ADD CONSTRAINT runtime_execution_specs_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_execution_specs
    ADD CONSTRAINT runtime_execution_specs_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_external_receipts
    ADD CONSTRAINT runtime_external_receipts_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_external_receipts
    ADD CONSTRAINT runtime_external_receipts_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_memory_namespaces
    ADD CONSTRAINT runtime_memory_namespaces_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agents(id)""",

    """ALTER TABLE ONLY runtime_memory_namespaces
    ADD CONSTRAINT runtime_memory_namespaces_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY runtime_node_quiescence_evidence
    ADD CONSTRAINT runtime_node_quiescence_evidence_task_id_execution_epoch_fkey FOREIGN KEY (task_id, execution_epoch) REFERENCES runtime_resource_ledgers(task_id, execution_epoch)""",

    """ALTER TABLE ONLY runtime_permission_upgrades
    ADD CONSTRAINT runtime_permission_upgrades_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY runtime_permission_upgrades
    ADD CONSTRAINT runtime_permission_upgrades_command_id_fkey FOREIGN KEY (command_id) REFERENCES runtime_commands(id)""",

    """ALTER TABLE ONLY runtime_permission_upgrades
    ADD CONSTRAINT runtime_permission_upgrades_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_permission_upgrades
    ADD CONSTRAINT runtime_permission_upgrades_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_projection_cursors
    ADD CONSTRAINT runtime_projection_cursors_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY runtime_projection_cursors
    ADD CONSTRAINT runtime_projection_cursors_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_resource_launch_resolutions
    ADD CONSTRAINT runtime_resource_launch_resol_task_id_execution_epoch_regi_fkey FOREIGN KEY (task_id, execution_epoch, registration_seq) REFERENCES runtime_resource_registrations(task_id, execution_epoch, registration_seq)""",

    """ALTER TABLE ONLY runtime_resource_ledgers
    ADD CONSTRAINT runtime_resource_ledgers_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_resource_ledgers
    ADD CONSTRAINT runtime_resource_ledgers_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_resource_ledgers
    ADD CONSTRAINT runtime_resource_ledgers_worker_id_fkey FOREIGN KEY (worker_id) REFERENCES runtime_worker_incarnations(worker_id)""",

    """ALTER TABLE ONLY runtime_resource_registrations
    ADD CONSTRAINT runtime_resource_registrations_task_id_execution_epoch_fkey FOREIGN KEY (task_id, execution_epoch) REFERENCES runtime_resource_ledgers(task_id, execution_epoch)""",

    """ALTER TABLE ONLY runtime_sandbox_attestations
    ADD CONSTRAINT runtime_sandbox_attestations_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_sandbox_attestations
    ADD CONSTRAINT runtime_sandbox_attestations_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_sessions
    ADD CONSTRAINT runtime_sessions_active_task_id_fkey FOREIGN KEY (active_task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_sessions
    ADD CONSTRAINT runtime_sessions_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY runtime_snapshots
    ADD CONSTRAINT runtime_snapshots_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_snapshots
    ADD CONSTRAINT runtime_snapshots_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY runtime_task_executions
    ADD CONSTRAINT runtime_task_executions_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY runtime_task_executions
    ADD CONSTRAINT runtime_task_executions_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY schedule_occurrences
    ADD CONSTRAINT schedule_occurrences_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY schedule_occurrences
    ADD CONSTRAINT schedule_occurrences_schedule_id_fkey FOREIGN KEY (schedule_id) REFERENCES schedules(id)""",

    """ALTER TABLE ONLY schedule_occurrences
    ADD CONSTRAINT schedule_occurrences_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY schedules
    ADD CONSTRAINT schedules_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agents(id)""",

    """ALTER TABLE ONLY schedules
    ADD CONSTRAINT schedules_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY scope_closure
    ADD CONSTRAINT scope_closure_ancestor_id_fkey FOREIGN KEY (ancestor_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY scope_closure
    ADD CONSTRAINT scope_closure_descendant_id_fkey FOREIGN KEY (descendant_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY scope_memberships
    ADD CONSTRAINT scope_memberships_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY scope_memberships
    ADD CONSTRAINT scope_memberships_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY scopes
    ADD CONSTRAINT scopes_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY scopes
    ADD CONSTRAINT scopes_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY security_audit_events
    ADD CONSTRAINT security_audit_events_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY security_audit_events
    ADD CONSTRAINT security_audit_events_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES scopes(id) ON DELETE SET NULL""",

    """ALTER TABLE ONLY security_audit_events
    ADD CONSTRAINT security_audit_events_target_user_id_fkey FOREIGN KEY (target_user_id) REFERENCES users(id)""",

    """ALTER TABLE ONLY skill_upload_sources
    ADD CONSTRAINT skill_upload_sources_artifact_digest_fkey FOREIGN KEY (artifact_digest) REFERENCES skill_artifacts(digest)""",

    """ALTER TABLE ONLY skill_upload_sources
    ADD CONSTRAINT skill_upload_sources_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES scopes(id)""",

    """ALTER TABLE ONLY skill_upload_sources
    ADD CONSTRAINT skill_upload_sources_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES users(id)""",

    """ALTER TABLE ONLY task_artifact_deliveries
    ADD CONSTRAINT task_artifact_deliveries_artifact_id_fkey FOREIGN KEY (artifact_id) REFERENCES artifacts(id)""",

    """ALTER TABLE ONLY task_artifact_deliveries
    ADD CONSTRAINT task_artifact_deliveries_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY task_events
    ADD CONSTRAINT task_events_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY task_events
    ADD CONSTRAINT task_events_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY task_messages
    ADD CONSTRAINT task_messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY task_messages
    ADD CONSTRAINT task_messages_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY task_projection_states
    ADD CONSTRAINT task_projection_states_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY task_projection_states
    ADD CONSTRAINT task_projection_states_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY task_usage_scopes
    ADD CONSTRAINT task_usage_scopes_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE""",

    """ALTER TABLE ONLY tasks
    ADD CONSTRAINT tasks_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY tool_execution_receipts
    ADD CONSTRAINT tool_execution_receipts_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY users
    ADD CONSTRAINT users_disabled_by_fkey FOREIGN KEY (disabled_by) REFERENCES users(id)""",

    """ALTER TABLE ONLY workspace_checkpoints
    ADD CONSTRAINT workspace_checkpoints_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id)""",

    """ALTER TABLE ONLY workspace_checkpoints
    ADD CONSTRAINT workspace_checkpoints_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY workspace_release_requests
    ADD CONSTRAINT workspace_release_requests_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

    """ALTER TABLE ONLY workspace_residencies
    ADD CONSTRAINT workspace_residencies_last_task_id_fkey FOREIGN KEY (last_task_id) REFERENCES tasks(id)""",

    """ALTER TABLE ONLY workspace_residencies
    ADD CONSTRAINT workspace_residencies_session_id_fkey FOREIGN KEY (session_id) REFERENCES runtime_sessions(id)""",

)


SEED_STATEMENTS = (
    """INSERT INTO scopes (id, kind, parent_id, owner_user_id, name, created_at, updated_at, normalized_name, tree_version) VALUES ('00000000-0000-4000-8000-000000000001', 'organization', NULL, NULL, 'workmate', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'workmate', 1)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('e1ddcf06-7f62-485f-b7b1-86cc5bf6368b', 'agent.view', '', 'agent.view', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('3d95844b-b168-4850-9edd-51dc07e9a2d9', 'agent.use', '', 'agent.use', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('bf95135a-e0e0-46a5-8e27-6b2a7b7807ac', 'agent.create', '', 'agent.create', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('72a43586-330d-4563-bd4b-f7b0d9ec1753', 'agent.update', '', 'agent.update', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('d95f12e5-3a21-47b1-94d8-5829e2934027', 'agent.delete', '', 'agent.delete', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('093163de-38ae-4991-911c-4447a25b8193', 'agent.publish', '', 'agent.publish', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('4e4b4d63-5c06-482e-a5a1-1d2a2dda25b9', 'skill.manage', '', 'skill.manage', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('818b9c08-5b94-4fd0-afb8-b6495b89cdb6', 'mcp.manage', '', 'mcp.manage', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('1babbf77-afcf-42fc-9385-771d7b40d0de', 'role.bind', '', 'role.bind', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('06cd8298-7e9d-4f8b-9a87-80e8579b8f37', 'scope.manage', '', 'scope.manage', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('4df33cae-4383-5b57-9740-970d4522c91f', 'user.read', '', 'user.read', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('0a8db6b8-0f6b-530f-a527-cb33c0d81676', 'user.manage', '', 'user.manage', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('6923a304-eb12-5cb3-865a-c00900aa23d7', 'membership.read', '', 'membership.read', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('7564b53b-e8e2-5a3d-a41b-a1159361b5b9', 'membership.manage', '', 'membership.manage', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('22e711db-7f3b-5311-b98d-30841d2089a3', 'role.read', '', 'role.read', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('76ed2086-27cd-5777-8434-8abcfeef44bc', 'role.manage', '', 'role.manage', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('6e5a1da8-bf9a-574a-8af6-f09f8bb6fe04', 'audit.read', '', 'audit.read', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('6b75fc36-1c4c-4129-930b-e441b8ac4e50', 'usage.read', '', '用量与成本查看', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('a543e4d4-8c23-4c8d-bcc9-f3d61e341a31', 'knowledge.manage', '', '知识库管理', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO permissions (id, key, description, display_name, is_system, created_at) VALUES ('c1b22ca2-db4c-4abe-a674-7882519eeb90', 'channel.manage', 'Organization IM connection management', '管理消息渠道', true, CURRENT_TIMESTAMP)""",
    """INSERT INTO roles (id, key, display_name, description, is_system, created_at, updated_at, version, delegatable) VALUES ('f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', 'admin', '组织管理员', '', true, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, false)""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('608cae4c-b4c0-4b76-a677-7470f7a47424', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', 'e1ddcf06-7f62-485f-b7b1-86cc5bf6368b')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('6869672a-a654-4999-91b2-a44d061536ea', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '3d95844b-b168-4850-9edd-51dc07e9a2d9')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('5aec932c-a389-4e0b-aba5-3a3fa2a39e4a', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', 'bf95135a-e0e0-46a5-8e27-6b2a7b7807ac')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('e05666e0-d0a6-44f0-9a56-eb111f16194f', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '72a43586-330d-4563-bd4b-f7b0d9ec1753')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('25c72627-93a3-41dd-9b4a-4102a7104cd4', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', 'd95f12e5-3a21-47b1-94d8-5829e2934027')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('edc37617-7030-4c6f-97cd-d647a169ac48', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '093163de-38ae-4991-911c-4447a25b8193')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('b06d01e9-c989-451e-92e7-6964184c2426', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '4e4b4d63-5c06-482e-a5a1-1d2a2dda25b9')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('ec17df16-3638-491e-928a-b61ca24d9433', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '818b9c08-5b94-4fd0-afb8-b6495b89cdb6')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('5451daf0-6731-4a75-b2cd-0c3b78dc616a', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '1babbf77-afcf-42fc-9385-771d7b40d0de')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('50f9aff5-4add-40f3-b536-f47f53535c42', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '06cd8298-7e9d-4f8b-9a87-80e8579b8f37')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('da3dbb4f-f229-56e5-9f36-2da72c2746d0', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '4df33cae-4383-5b57-9740-970d4522c91f')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('724ed535-a597-5aa1-b6c8-3c56b408ea5d', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '0a8db6b8-0f6b-530f-a527-cb33c0d81676')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('d00b2968-c808-5ab5-8268-3dba447cfec9', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '6923a304-eb12-5cb3-865a-c00900aa23d7')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('20be66a0-fb35-52ce-8c22-8aa1aa1ecf87', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '7564b53b-e8e2-5a3d-a41b-a1159361b5b9')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('f7ef5cd4-bfd0-53d5-9eb4-8488ba8883b1', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '22e711db-7f3b-5311-b98d-30841d2089a3')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('2b4fc376-b19d-59db-9566-a6680a93a429', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '76ed2086-27cd-5777-8434-8abcfeef44bc')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('bea21cd1-1276-5075-9bbc-70b734f8019f', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '6e5a1da8-bf9a-574a-8af6-f09f8bb6fe04')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('b3af4c0b-e91d-4f19-8544-8489a192b5d4', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', '6b75fc36-1c4c-4129-930b-e441b8ac4e50')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('8ef22ad8-5189-4d5d-b0f0-8a85c45f154d', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', 'a543e4d4-8c23-4c8d-bcc9-f3d61e341a31')""",
    """INSERT INTO role_permissions (id, role_id, permission_id) VALUES ('4e6a6d8c-e544-44f5-8643-8b6957e4f939', 'f23ac1f5-0d9c-4bf3-921e-e4351c03bdff', 'c1b22ca2-db4c-4abe-a674-7882519eeb90')""",
    """INSERT INTO scope_closure (ancestor_id, descendant_id, depth) VALUES ('00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000001', 0)""",
)
