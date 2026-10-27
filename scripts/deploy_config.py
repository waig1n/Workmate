"""Pure deployment validation of YAML and Docker's resolved Compose model.

Never parses .env, reads process environment, launches services, or reports
configuration values. The caller owns execution and output redaction.
"""

from dataclasses import dataclass, field
import re
import json
from typing import Literal
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class Finding:
    level: Literal["error", "warning", "ok"]
    code: str
    message: str
    security: bool = False


@dataclass(frozen=True)
class Plan:
    profiles: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    apps: tuple[str, ...] = ("gateway", "runtime", "frontend")
    init_services: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()
    secrets: tuple[str, ...] = field(default=(), repr=False)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _environment(service):
    raw = _mapping(service).get("environment")
    if isinstance(raw, dict):
        return {str(k): str(v) if v is not None else "" for k, v in raw.items()}
    # docker compose config normally emits mappings; accept list-form models
    # as a convenience for programmatic callers, without resolving variables.
    if isinstance(raw, list):
        return {
            v.partition("=")[0]: v.partition("=")[2] for v in raw if isinstance(v, str)
        }
    return {}


def _url(value):
    try:
        result = urlsplit(value)
        _ = result.port  # invalid numeric ports must also fail validation
        return result if result.scheme and result.hostname else None
    except (ValueError, TypeError):
        return None


def evaluate(app_config: dict, compose_config: dict) -> Plan:
    """Return an explicit dependency/app/init plan without side effects.

    Missing optional model credentials are warnings; the default model and
    enabled knowledge dependencies are required. Findings contain field names
    and repair advice, never environment values or URL credentials.
    """
    findings = []
    dependencies = []
    profiles = []
    init_services = []
    services = _mapping(_mapping(compose_config).get("services"))
    envs = {name: _environment(service) for name, service in services.items()}
    runtime = envs.get("runtime", {})
    product = envs.get("gateway", {})
    product_config = _mapping(_mapping(app_config).get("product"))
    secrets = set()
    for environment in envs.values():
        for name, value in environment.items():
            if value and re.search(r"KEY|TOKEN|PASSWORD|SECRET", name, re.I):
                secrets.add(value)
            # Even a malformed URL can leak credentials in Docker/SDK errors.
            match = re.search(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/@:]*:([^/@]*)@", value)
            if match and match.group(1):
                secrets.update((match.group(1), unquote(match.group(1))))

    def report(code, message, level="error", security=False):
        findings.append(Finding(level, code, message, security=security))

    def require(environment, name, code="environment_missing", level="error"):
        value = environment.get(name, "").strip()
        if not value:
            report(
                code, f"Required environment mapping {name} is empty or absent.", level
            )
        return value

    def endpoint(environment, name, schemes=("http", "https"), level="error"):
        value = require(environment, name, level=level)
        if not value:
            return None
        parsed = _url(value)
        if parsed is None or parsed.scheme not in schemes:
            report("url_invalid", f"{name} must contain a valid service URL.", level)
            return None
        if schemes == ("http", "https") and (parsed.username or parsed.password):
            report(
                "url_credentials_forbidden",
                f"{name} must not embed credentials.",
                level,
            )
            return None
        return parsed

    def select(name, initialization=False):
        if name not in services:
            report("service_missing", f"Required Compose service {name} is missing.")
            return
        destination = init_services if initialization else dependencies
        if name not in destination:
            destination.append(name)
        declared = _mapping(services[name]).get("profiles", [])
        if isinstance(declared, list):
            for profile in declared:
                if isinstance(profile, str) and profile not in profiles:
                    profiles.append(profile)

    for feature in ("web", "knowledge"):
        enabled = _mapping(product_config.get(feature)).get("enabled", False)
        if not isinstance(enabled, bool):
            report(
                "feature_enabled_invalid",
                f"product.{feature}.enabled must be a boolean.",
            )

    for name in Plan().apps:
        if name not in services:
            report(
                "service_missing", f"Required Compose application {name} is missing."
            )

    jwt = require(product, "JWT_SECRET")
    if jwt and (
        len(jwt) < 32
        or re.search(
            r"change.?me|placeholder|example|your[-_ ]?secret|replace.?me", jwt, re.I
        )
    ):
        report(
            "jwt_secret_weak",
            "JWT_SECRET is short or a placeholder; use at least 32 random characters for deployment.",
            "warning",
            security=True,
        )
    token = require(runtime, "WORKMATE_INTERNAL_TOKEN")
    peer = require(product, "RUNTIME_INTERNAL_TOKEN")
    if token and (
        len(token) < 32
        or re.search(r"change.?me|placeholder|example|replace.?me", token, re.I)
    ):
        report(
            "internal_token_weak",
            "Internal service token is short or a placeholder; use at least 32 random characters.",
            "warning",
            security=True,
        )
    if token and peer and token != peer:
        report(
            "internal_token_mismatch",
            "Runtime and Product internal service tokens must match.",
        )

    worker_db = require(runtime, "DATABASE_URL")
    if worker_db and worker_db != product.get("DATABASE_URL"):
        report("worker_database_mismatch", "Worker and Product must use the same DATABASE_URL.")
    artifact_policy = _mapping(_mapping(product_config.get("artifacts")).get("storage"))
    artifact_backend = product.get("ARTIFACT_STORAGE_BACKEND") or artifact_policy.get("backend", "local")
    worker_backend = runtime.get("ARTIFACT_STORAGE_BACKEND") or artifact_policy.get("backend", "local")
    if artifact_backend not in {"local", "s3"} or worker_backend != artifact_backend:
        report("artifact_backend_invalid", "Artifact backend must be local or s3 and match across roles.")
    try:
        nodes = json.loads(product.get("WORKMATE_WORKER_NODES") or "[]")
        if not isinstance(nodes, list):
            raise ValueError()
        identities = set()
        for node in nodes:
            if not isinstance(node, dict) or set(node) - {"storage_node_id", "base_url", "enabled"}:
                raise ValueError()
            identity = node.get("storage_node_id")
            origin = node.get("base_url")
            address = _url(origin)
            # Keep this dependency-free check aligned with Product config.nodes.WorkerNode.
            if (not isinstance(identity, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', identity) is None
                    or identity in identities or not isinstance(origin, str)
                    or address is None or address.scheme not in {"http", "https"}
                    or address.username is not None or address.password is not None
                    or address.path not in {'', '/'} or '?' in origin or '#' in origin
                    or any(character.isspace() for character in origin)
                    or (address.port is not None and not 1 <= address.port <= 65535)
                    or not isinstance(node.get("enabled", True), bool)):
                raise ValueError()
            identities.add(identity)
    except (ValueError, TypeError):
        nodes = []
        report("worker_nodes_invalid", "WORKMATE_WORKER_NODES must be a JSON list of unique node identities, service URLs and boolean enabled flags.")
    if nodes and artifact_backend != "s3":
        report("worker_nodes_require_s3", "Worker node directory requires S3 artifact storage.")
    require(runtime, "WORKMATE_WORKER_STORAGE_NODE_ID")
    fields = []
    if not nodes:
        fields.append("WORKMATE_WORKER_STORAGE_NODE_ID")
    if artifact_backend == "local":
        fields.append("ARTIFACT_STORAGE_DIR")
    for name in fields:
        worker_value, product_value = require(runtime, name), require(product, name)
        if worker_value and product_value and worker_value != product_value:
            report("worker_storage_mismatch", f"Single-node local storage must share {name}.")
    knowledge_endpoint = endpoint(product, "RUNTIME_BASE_URL")
    if knowledge_endpoint and knowledge_endpoint.hostname == "agentscope-runtime":
        report("legacy_runtime_endpoint", "Native Product must target the Worker knowledge endpoint, not the legacy App.")

    database_urls = []
    postgres = envs.get("postgres", {})
    for environment, name, expected_database in (
        (product, "DATABASE_URL", postgres.get("POSTGRES_DB", "")),
        (runtime, "AS_DATABASE_URL", "workmate_agentscope"),
    ):
        parsed = endpoint(
            environment, name, ("postgresql", "postgresql+asyncpg", "postgres")
        )
        database_urls.append(parsed)
        if not parsed:
            continue
        if not parsed.path.lstrip("/"):
            report("database_name_missing", f"{name} must select a named database.")
        if parsed.hostname == "postgres":
            select("postgres")
            for field_name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
                require(postgres, field_name)
            if (
                unquote(parsed.username or "") != postgres.get("POSTGRES_USER")
                or unquote(parsed.password or "") != postgres.get("POSTGRES_PASSWORD")
                or unquote(parsed.path.lstrip("/")) != expected_database
            ):
                report(
                    "database_identity_mismatch",
                    f"{name} does not match the local Postgres user, password or provisioned database.",
                )
    if all(database_urls):
        left, right = database_urls
        if (left.hostname, left.port or 5432, unquote(left.path)) == (
            right.hostname,
            right.port or 5432,
            unquote(right.path),
        ):
            report(
                "database_not_isolated",
                "Product and Runtime must use separate databases.",
            )
    redis = endpoint(runtime, "AS_REDIS_URL", ("redis", "rediss"))
    if redis and redis.hostname == "redis":
        select("redis")
    for environment in (product,runtime):
        coordination=endpoint(environment,'WORKMATE_COORDINATION_REDIS_URL',('redis','rediss'))
        if coordination and coordination.hostname=='redis':
            select('redis')
    if (product.get('WORKMATE_COORDINATION_REDIS_URL') and runtime.get('WORKMATE_COORDINATION_REDIS_URL')
            and product['WORKMATE_COORDINATION_REDIS_URL']!=runtime['WORKMATE_COORDINATION_REDIS_URL']):
        report('command_coordination_mismatch','Gateway and Runtime must use the same command Redis connection.')
    namespaces=[environment.get('WORKMATE_COORDINATION_NAMESPACE','workmate') for environment in (product,runtime)]
    if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value) for value in namespaces):
        report('command_namespace_invalid','Command namespace must contain 1-64 letters, digits, underscores or hyphens.')
    if namespaces[0]!=namespaces[1]:
        report('command_namespace_mismatch','Gateway and Runtime must use the same command namespace.')

    # Skill packages are required even when knowledge retrieval is disabled.
    skill_endpoint = endpoint(runtime, 'SKILL_S3_ENDPOINT')
    endpoint(product, 'SKILL_S3_ENDPOINT')
    for key in ('ENDPOINT', 'BUCKET', 'ACCESS_KEY', 'SECRET_KEY'):
        name = 'SKILL_S3_' + key
        left, right = require(runtime, name), require(product, name)
        if left != right:
            report('skill_storage_mismatch', f'Product and Worker must share {name}.')
    if skill_endpoint and (skill_endpoint.query or skill_endpoint.fragment):
        report('url_invalid', 'Skill S3 endpoint cannot contain query or fragment.')
    if skill_endpoint and skill_endpoint.hostname == 'rustfs':
        select('rustfs')
        select('storage-init', initialization=True)
        root, initial = envs.get('rustfs', {}), envs.get('storage-init', {})
        if len(root.get('RUSTFS_ACCESS_KEY', '')) < 3 or len(root.get('RUSTFS_SECRET_KEY', '')) < 8:
            report('rustfs_root_invalid', 'Local RustFS requires a nonempty root account and strong secret.')
        if (runtime.get('SKILL_S3_ACCESS_KEY') != root.get('RUSTFS_ACCESS_KEY')
                or runtime.get('SKILL_S3_SECRET_KEY') != root.get('RUSTFS_SECRET_KEY')):
            report('skill_rustfs_credentials_mismatch', 'Local Skill credentials must match the configured RustFS root account.')
        if any(initial.get('SKILL_S3_'+key) != runtime.get('SKILL_S3_'+key)
               for key in ('ENDPOINT', 'BUCKET', 'ACCESS_KEY', 'SECRET_KEY')):
            report('skill_bucket_init_mismatch', 'Skill bucket initialization must match the Skill application configuration.')

    if artifact_backend == "s3":
        artifact_endpoint = endpoint(runtime, "ARTIFACT_S3_ENDPOINT")
        endpoint(product, "ARTIFACT_S3_ENDPOINT")
        for key in ("ENDPOINT", "BUCKET", "ACCESS_KEY", "SECRET_KEY"):
            name = "ARTIFACT_S3_" + key
            left, right = require(runtime, name), require(product, name)
            if left != right:
                report("artifact_storage_mismatch", f"Product and Worker must share {name}.")
        if artifact_endpoint and (artifact_endpoint.query or artifact_endpoint.fragment):
            report("url_invalid", "Artifact S3 endpoint cannot contain query or fragment.")
        for prefix in ("SKILL", "KNOWLEDGE"):
            if (runtime.get("ARTIFACT_S3_ENDPOINT", "").rstrip("/") == runtime.get(prefix + "_S3_ENDPOINT", "").rstrip("/")
                    and runtime.get("ARTIFACT_S3_BUCKET") == runtime.get(prefix + "_S3_BUCKET")):
                report("artifact_bucket_overlap", "Artifacts must use a private bucket separate from Skill and knowledge.")
        if artifact_endpoint and artifact_endpoint.hostname == "rustfs":
            select("rustfs")
            select("storage-init", initialization=True)
            initial, root = envs.get("storage-init", {}), envs.get("rustfs", {})
            if any(initial.get("ARTIFACT_S3_" + key) != runtime.get("ARTIFACT_S3_" + key)
                   for key in ("ENDPOINT", "BUCKET", "ACCESS_KEY", "SECRET_KEY")):
                report("artifact_bucket_init_mismatch", "Artifact initialization must match application object storage configuration.")
            if (runtime.get("ARTIFACT_S3_ACCESS_KEY") != root.get("RUSTFS_ACCESS_KEY")
                    or runtime.get("ARTIFACT_S3_SECRET_KEY") != root.get("RUSTFS_SECRET_KEY")):
                report("artifact_rustfs_credentials_mismatch", "Local Artifact credentials must match configured RustFS credentials.")

    models = _mapping(product_config.get("models"))
    model_profiles = _mapping(models.get("profiles"))
    default = models.get("default")
    if not isinstance(default, str) or default not in model_profiles:
        report(
            "default_model_missing",
            "product.models.default must name a declared model profile.",
        )

    def validate_model(profile, environments, level):
        profile = _mapping(profile)
        reference = profile.get("api_key_env")
        if not isinstance(reference, str) or not re.fullmatch(
            r"[A-Z_][A-Z0-9_]*", reference
        ):
            report(
                "model_environment_reference_invalid",
                "A model api_key_env must name an environment variable.",
                level,
            )
        else:
            values = [
                require(environment, reference, "model_environment_missing", level)
                for environment in environments
            ]
            secrets.update(value for value in values if value)
            if all(values) and len(set(values)) > 1:
                report(
                    "model_environment_mismatch",
                    f"{reference} differs between Product and Runtime.",
                    level,
                )
        reference = profile.get("base_url_env")
        if reference:
            if not isinstance(reference, str) or not re.fullmatch(
                r"[A-Z_][A-Z0-9_]*", reference
            ):
                report(
                    "model_environment_reference_invalid",
                    "A model base_url_env must name an environment variable.",
                    level,
                )
            else:
                values = []
                for environment in environments:
                    # An explicit reference must exist in Compose even when
                    # empty (SDK provider default is allowed in that case).
                    if reference not in environment:
                        report(
                            "model_environment_missing",
                            f"{reference} is not passed into the application container.",
                            level,
                        )
                    elif environment[reference].strip():
                        endpoint(environment, reference, level=level)
                    values.append(environment.get(reference, "").strip())
                if len(set(values)) > 1:
                    report(
                        "model_environment_mismatch",
                        f"{reference} differs between Product and Runtime.",
                        level,
                    )

    for name, profile in model_profiles.items():
        validate_model(
            profile, (product, runtime), "error" if name == default else "warning"
        )

    web = _mapping(product_config.get("web"))
    if web.get("enabled") is True:
        tools = web.get("tools")
        if not isinstance(tools, list) or not tools:
            report("web_tools_missing", "Enabled Web access requires configured tools.")
        else:
            for tool in tools:
                reference = _mapping(tool).get("base_url_env")
                if not isinstance(reference, str) or not re.fullmatch(
                    r"[A-Z_][A-Z0-9_]*", reference
                ):
                    report(
                        "web_environment_reference_invalid",
                        "Web tools must declare a valid base_url_env reference.",
                    )
                    continue
                parsed = endpoint(runtime, reference)
                if parsed and parsed.hostname in {"searxng", "crawl4ai"}:
                    select(parsed.hostname)
                token_reference = _mapping(tool).get("token_env")
                if parsed and parsed.hostname == "crawl4ai":
                    server_token = require(
                        envs.get("crawl4ai", {}), "CRAWL4AI_API_TOKEN"
                    )
                    client_token = (
                        runtime.get(token_reference, "")
                        if isinstance(token_reference, str)
                        else ""
                    )
                    if not client_token or client_token != server_token:
                        report(
                            "web_fetch_token_mismatch",
                            "Local Crawl4AI requires a nonempty server token matching Runtime tool token_env; set WEB_FETCH_API_TOKEN for both.",
                        )
                if isinstance(token_reference, str) and token_reference not in runtime:
                    report(
                        "web_token_mapping_missing",
                        "The configured Web token environment reference is not passed into Runtime.",
                    )
        if "searxng" in dependencies:
            require(envs.get("searxng", {}), "SEARXNG_SECRET")

    knowledge = _mapping(product_config.get("knowledge"))
    if knowledge.get("enabled") is True:
        embedding = _mapping(knowledge.get("embedding"))
        validate_model(embedding, (runtime,), "error")
        dimensions_mode = embedding.get("dimensions_mode", "auto")
        if dimensions_mode not in ("auto", "send", "omit"):
            report(
                "embedding_dimensions_mode_invalid",
                "dimensions_mode must be auto, send, or omit.",
            )
        dimensions = embedding.get("dimensions")
        if (
            isinstance(dimensions, bool)
            or not isinstance(dimensions, int)
            or dimensions <= 0
        ):
            report(
                "embedding_dimensions_invalid",
                "Knowledge requires a positive integer embedding dimension.",
            )
        if not embedding.get("model") or embedding.get("type") not in {
            "openai",
            "dashscope",
        }:
            report(
                "embedding_model_invalid",
                "Knowledge requires product.knowledge.embedding; the ReMe embedding is not used.",
            )
        qdrant = endpoint(runtime, "KNOWLEDGE_QDRANT_URL")
        qdrant_key = require(runtime, "KNOWLEDGE_QDRANT_API_KEY")
        if qdrant and qdrant.hostname == "qdrant":
            select("qdrant")
            if qdrant_key != envs.get("qdrant", {}).get("QDRANT__SERVICE__API_KEY"):
                report(
                    "qdrant_key_mismatch",
                    "Local Qdrant and Runtime must use the same API key.",
                )
        s3 = endpoint(runtime, "KNOWLEDGE_S3_ENDPOINT")
        access = require(runtime, "KNOWLEDGE_S3_ACCESS_KEY")
        secret = require(runtime, "KNOWLEDGE_S3_SECRET_KEY")
        bucket = require(runtime, "KNOWLEDGE_S3_BUCKET")
        if (runtime.get('SKILL_S3_ENDPOINT', '').rstrip('/') == runtime.get('KNOWLEDGE_S3_ENDPOINT', '').rstrip('/')
                and runtime.get('SKILL_S3_BUCKET') == bucket):
            report('skill_knowledge_bucket_overlap', 'Skill and knowledge must use separate private buckets.')
        if s3 and s3.hostname == "rustfs":
            select("rustfs")
            select("storage-init", initialization=True)
            root = envs.get("rustfs", {})
            if (
                len(root.get("RUSTFS_ACCESS_KEY", "")) < 3
                or len(root.get("RUSTFS_SECRET_KEY", "")) < 8
            ):
                report(
                    "rustfs_root_invalid",
                    "Local RustFS root user needs at least 3 characters and password at least 8.",
                )
            if access != root.get("RUSTFS_ACCESS_KEY") or secret != root.get(
                "RUSTFS_SECRET_KEY"
            ):
                report(
                    "rustfs_credentials_mismatch",
                    "Local RustFS and Runtime credentials must match.",
                )
            initial = envs.get("storage-init", {})
            if any(
                initial.get(name) != value
                for name, value in (
                    ("KNOWLEDGE_S3_ENDPOINT", runtime.get("KNOWLEDGE_S3_ENDPOINT")),
                    ("KNOWLEDGE_S3_ACCESS_KEY", access),
                    ("KNOWLEDGE_S3_SECRET_KEY", secret),
                    ("KNOWLEDGE_S3_BUCKET", bucket),
                )
            ):
                report(
                    "rustfs_init_mismatch",
                    "RustFS initialization must use the Runtime endpoint, credentials and bucket.",
                )

    # Keep deterministic startup order irrespective of profile/tool declaration
    # order. The caller starts exactly these services using --no-deps.
    order = ("postgres", "redis", "searxng", "crawl4ai", "qdrant", "rustfs")
    dependencies = [name for name in order if name in dependencies]
    profiles = []
    for name in (*dependencies, *init_services):
        declared = _mapping(services[name]).get("profiles", [])
        if not isinstance(declared, list):
            report(
                "service_profiles_invalid",
                f"Compose service {name} has invalid profiles.",
            )
            continue
        for profile in declared:
            if isinstance(profile, str) and profile not in profiles:
                profiles.append(profile)
    if not any(f.level == "error" for f in findings):
        report(
            "configuration_ready",
            "Resolved configuration passed deployment preflight.",
            "ok",
        )
    return Plan(
        tuple(profiles),
        tuple(dependencies),
        init_services=tuple(init_services),
        findings=tuple(findings),
        secrets=tuple(sorted(secrets, key=lambda s: (-len(s), s))),
    )
