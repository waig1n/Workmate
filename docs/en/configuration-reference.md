# Parameter reference

[English](../en/configuration-reference.md) | [简体中文](../zh-CN/configuration-reference.md)

This reference lists all settings and their configured values in the YAML template. Suffixes identify seconds (`_seconds` / `_secs`), milliseconds (`_ms`), bytes (`_bytes`), and characters (`_chars`). Interpret empty values and lists using the field schema; they do not universally mean disabled. Source links point to the corresponding configuration comments. Configuration schemas define valid types and ranges.

[Configuration guide](configuration.md)

| Parameter | Configured value | Source |
|---|---|---|
| `version` | `1` | [YAML](../../config/workmate.yaml#L11) |
| `product.channels.enabled` | `true` | [YAML](../../config/workmate.yaml#L17) |
| `product.channels.wechat_enabled` | `false` | [YAML](../../config/workmate.yaml#L19) |
| `product.channels.reply_delay_secs` | `30` | [YAML](../../config/workmate.yaml#L21) |
| `product.channels.stream_interval_secs` | `1` | [YAML](../../config/workmate.yaml#L23) |
| `product.channels.max_live_replies` | `64` | [YAML](../../config/workmate.yaml#L25) |
| `product.channels.poll_interval_secs` | `2` | [YAML](../../config/workmate.yaml#L27) |
| `product.channels.batch_size` | `20` | [YAML](../../config/workmate.yaml#L29) |
| `product.channels.lease_secs` | `60` | [YAML](../../config/workmate.yaml#L31) |
| `product.channels.max_connections` | `32` | [YAML](../../config/workmate.yaml#L33) |
| `product.channels.max_inbox_pending` | `1000` | [YAML](../../config/workmate.yaml#L35) |
| `product.channels.message_max_chars` | `16000` | [YAML](../../config/workmate.yaml#L37) |
| `product.schedules.enabled` | `true` | [YAML](../../config/workmate.yaml#L41) |
| `product.schedules.poll_interval_secs` | `5` | [YAML](../../config/workmate.yaml#L43) |
| `product.schedules.batch_size` | `20` | [YAML](../../config/workmate.yaml#L45) |
| `product.schedules.misfire_grace_secs` | `300` | [YAML](../../config/workmate.yaml#L47) |
| `product.knowledge.enabled` | `true` | [YAML](../../config/workmate.yaml#L51) |
| `product.knowledge.embedding.type` | `openai` | [YAML](../../config/workmate.yaml#L55) |
| `product.knowledge.embedding.model` | `BAAI/bge-m3` | [YAML](../../config/workmate.yaml#L57) |
| `product.knowledge.embedding.dimensions` | `1024` | [YAML](../../config/workmate.yaml#L59) |
| `product.knowledge.embedding.dimensions_mode` | `auto` | [YAML](../../config/workmate.yaml#L62) |
| `product.knowledge.embedding.base_url_env` | `KNOWLEDGE_EMBEDDING_MODEL_BASE_URL` | [YAML](../../config/workmate.yaml#L64) |
| `product.knowledge.embedding.api_key_env` | `KNOWLEDGE_EMBEDDING_MODEL_API_KEY` | [YAML](../../config/workmate.yaml#L66) |
| `product.knowledge.chunk_size` | `1024` | [YAML](../../config/workmate.yaml#L68) |
| `product.knowledge.chunk_overlap` | `64` | [YAML](../../config/workmate.yaml#L70) |
| `product.knowledge.max_upload_bytes` | `20971520` | [YAML](../../config/workmate.yaml#L72) |
| `product.knowledge.search_timeout_secs` | `30` | [YAML](../../config/workmate.yaml#L74) |
| `product.usage.currency` | `CNY` | [YAML](../../config/workmate.yaml#L78) |
| `product.usage.prices` | `{}` | [YAML](../../config/workmate.yaml#L81) |
| `product.performance.event_queue_capacity` | `1024` | [YAML](../../config/workmate.yaml#L90) |
| `product.performance.task_max_outstanding` | `64` | [YAML](../../config/workmate.yaml#L92) |
| `product.performance.task_max_outstanding_per_user` | `8` | [YAML](../../config/workmate.yaml#L94) |
| `product.performance.task_max_pending_approval` | `256` | [YAML](../../config/workmate.yaml#L96) |
| `product.performance.task_max_pending_approval_per_user` | `32` | [YAML](../../config/workmate.yaml#L98) |
| `product.performance.approval_timeout_secs` | `86400` | [YAML](../../config/workmate.yaml#L100) |
| `product.auth.algorithm` | `HS256` | [YAML](../../config/workmate.yaml#L103) |
| `product.auth.expire_hours` | `168` | [YAML](../../config/workmate.yaml#L105) |
| `product.auth.registration_mode` | `bootstrap_only` | [YAML](../../config/workmate.yaml#L110) |
| `product.runtime.request_timeout_secs` | `30` | [YAML](../../config/workmate.yaml#L115) |
| `product.runtime.session_lock_ttl_secs` | `600` | [YAML](../../config/workmate.yaml#L117) |
| `product.builtin_skills.organization_use_ids[0]` | `academic-paper-review` | [YAML](../../config/workmate.yaml#L124) |
| `product.builtin_skills.organization_use_ids[1]` | `agent-bootstrap` | [YAML](../../config/workmate.yaml#L125) |
| `product.builtin_skills.organization_use_ids[2]` | `chart-visualization` | [YAML](../../config/workmate.yaml#L126) |
| `product.builtin_skills.organization_use_ids[3]` | `code-documentation` | [YAML](../../config/workmate.yaml#L127) |
| `product.builtin_skills.organization_use_ids[4]` | `consulting-analysis` | [YAML](../../config/workmate.yaml#L128) |
| `product.builtin_skills.organization_use_ids[5]` | `data-analysis` | [YAML](../../config/workmate.yaml#L129) |
| `product.builtin_skills.organization_use_ids[6]` | `deep-research` | [YAML](../../config/workmate.yaml#L130) |
| `product.builtin_skills.organization_use_ids[7]` | `find-skills` | [YAML](../../config/workmate.yaml#L131) |
| `product.builtin_skills.organization_use_ids[8]` | `frontend-design` | [YAML](../../config/workmate.yaml#L132) |
| `product.builtin_skills.organization_use_ids[9]` | `github-deep-research` | [YAML](../../config/workmate.yaml#L133) |
| `product.builtin_skills.organization_use_ids[10]` | `newsletter-generation` | [YAML](../../config/workmate.yaml#L134) |
| `product.builtin_skills.organization_use_ids[11]` | `ppt-generation` | [YAML](../../config/workmate.yaml#L135) |
| `product.builtin_skills.organization_use_ids[12]` | `skill-creator` | [YAML](../../config/workmate.yaml#L136) |
| `product.builtin_skills.organization_use_ids[13]` | `skill-reviewer` | [YAML](../../config/workmate.yaml#L137) |
| `product.builtin_skills.organization_use_ids[14]` | `surprise-me` | [YAML](../../config/workmate.yaml#L138) |
| `product.builtin_skills.organization_use_ids[15]` | `systematic-literature-review` | [YAML](../../config/workmate.yaml#L139) |
| `product.workspace_assistant.enabled` | `true` | [YAML](../../config/workmate.yaml#L144) |
| `product.web.enabled` | `true` | [YAML](../../config/workmate.yaml#L149) |
| `product.web.tools[0].name` | `web_search` | [YAML](../../config/workmate.yaml#L153) |
| `product.web.tools[0].group` | `web_access` | [YAML](../../config/workmate.yaml#L155) |
| `product.web.tools[0].use` | `workmate_runtime.web_providers:SearxngSearchProvider` | [YAML](../../config/workmate.yaml#L157) |
| `product.web.tools[0].base_url_env` | `WEB_SEARCH_BASE_URL` | [YAML](../../config/workmate.yaml#L159) |
| `product.web.tools[0].max_results` | `8` | [YAML](../../config/workmate.yaml#L161) |
| `product.web.tools[1].name` | `web_fetch` | [YAML](../../config/workmate.yaml#L163) |
| `product.web.tools[1].group` | `web_access` | [YAML](../../config/workmate.yaml#L165) |
| `product.web.tools[1].use` | `workmate_runtime.web_providers:Crawl4AIFetchProvider` | [YAML](../../config/workmate.yaml#L167) |
| `product.web.tools[1].base_url_env` | `WEB_FETCH_BASE_URL` | [YAML](../../config/workmate.yaml#L169) |
| `product.web.tools[1].token_env` | `WEB_FETCH_API_TOKEN` | [YAML](../../config/workmate.yaml#L171) |
| `product.web.tools[1].timeout_seconds` | `30` | [YAML](../../config/workmate.yaml#L173) |
| `product.web.tools[1].extract_mode` | `fit` | [YAML](../../config/workmate.yaml#L175) |
| `product.web.limits.max_response_bytes` | `5242880` | [YAML](../../config/workmate.yaml#L178) |
| `product.web.limits.max_extracted_chars` | `50000` | [YAML](../../config/workmate.yaml#L180) |
| `product.artifacts.storage.backend` | `local` | [YAML](../../config/workmate.yaml#L186) |
| `product.artifacts.storage.region` | `us-east-1` | [YAML](../../config/workmate.yaml#L188) |
| `product.artifacts.storage.timeout_seconds` | `30` | [YAML](../../config/workmate.yaml#L190) |
| `product.artifacts.storage.workspace_checkpoint_enabled` | `false` | [YAML](../../config/workmate.yaml#L192) |
| `product.artifacts.storage.workspace_checkpoint_max_files` | `1000` | [YAML](../../config/workmate.yaml#L194) |
| `product.artifacts.storage.workspace_checkpoint_max_file_bytes` | `16777216` | [YAML](../../config/workmate.yaml#L196) |
| `product.artifacts.storage.workspace_checkpoint_max_bytes` | `134217728` | [YAML](../../config/workmate.yaml#L198) |
| `product.artifacts.storage.tool_content_enabled` | `false` | [YAML](../../config/workmate.yaml#L200) |
| `product.artifacts.storage.tool_content_inline_bytes` | `65536` | [YAML](../../config/workmate.yaml#L202) |
| `product.artifacts.storage.tool_content_preview_chars` | `2048` | [YAML](../../config/workmate.yaml#L204) |
| `product.artifacts.max_file_bytes` | `52428800` | [YAML](../../config/workmate.yaml#L206) |
| `product.artifacts.max_files_per_run` | `20` | [YAML](../../config/workmate.yaml#L208) |
| `product.artifacts.preview_extensions[0]` | `pdf` | [YAML](../../config/workmate.yaml#L211) |
| `product.artifacts.preview_extensions[1]` | `txt` | [YAML](../../config/workmate.yaml#L212) |
| `product.artifacts.preview_extensions[2]` | `md` | [YAML](../../config/workmate.yaml#L213) |
| `product.artifacts.preview_extensions[3]` | `csv` | [YAML](../../config/workmate.yaml#L214) |
| `product.artifacts.preview_extensions[4]` | `json` | [YAML](../../config/workmate.yaml#L215) |
| `product.artifacts.preview_extensions[5]` | `yaml` | [YAML](../../config/workmate.yaml#L216) |
| `product.artifacts.preview_extensions[6]` | `yml` | [YAML](../../config/workmate.yaml#L217) |
| `product.artifacts.preview_extensions[7]` | `png` | [YAML](../../config/workmate.yaml#L218) |
| `product.artifacts.preview_extensions[8]` | `jpg` | [YAML](../../config/workmate.yaml#L219) |
| `product.artifacts.preview_extensions[9]` | `jpeg` | [YAML](../../config/workmate.yaml#L220) |
| `product.artifacts.preview_extensions[10]` | `webp` | [YAML](../../config/workmate.yaml#L221) |
| `product.artifacts.preview_extensions[11]` | `gif` | [YAML](../../config/workmate.yaml#L222) |
| `product.artifacts.force_download_extensions[0]` | `html` | [YAML](../../config/workmate.yaml#L226) |
| `product.artifacts.force_download_extensions[1]` | `htm` | [YAML](../../config/workmate.yaml#L227) |
| `product.artifacts.force_download_extensions[2]` | `xhtml` | [YAML](../../config/workmate.yaml#L228) |
| `product.artifacts.force_download_extensions[3]` | `svg` | [YAML](../../config/workmate.yaml#L229) |
| `product.artifacts.force_download_extensions[4]` | `xml` | [YAML](../../config/workmate.yaml#L230) |
| `product.artifacts.force_download_extensions[5]` | `js` | [YAML](../../config/workmate.yaml#L231) |
| `product.artifacts.force_download_extensions[6]` | `mjs` | [YAML](../../config/workmate.yaml#L232) |
| `product.artifacts.force_download_extensions[7]` | `zip` | [YAML](../../config/workmate.yaml#L233) |
| `product.artifacts.force_download_extensions[8]` | `tar` | [YAML](../../config/workmate.yaml#L234) |
| `product.artifacts.force_download_extensions[9]` | `gz` | [YAML](../../config/workmate.yaml#L235) |
| `product.models.default` | `deepseek-v4-flash` | [YAML](../../config/workmate.yaml#L239) |
| `product.models.media_profiles.text.model_inputs[0]` | `text` | [YAML](../../config/workmate.yaml#L245) |
| `product.models.media_profiles.text.pdf_strategy` | `extract_text` | [YAML](../../config/workmate.yaml#L247) |
| `product.models.media_profiles.vision-documents.model_inputs[0]` | `text` | [YAML](../../config/workmate.yaml#L251) |
| `product.models.media_profiles.vision-documents.model_inputs[1]` | `image` | [YAML](../../config/workmate.yaml#L251) |
| `product.models.media_profiles.vision-documents.pdf_strategy` | `extract_text` | [YAML](../../config/workmate.yaml#L253) |
| `product.models.profiles.deepseek-v4-flash.type` | `openai` | [YAML](../../config/workmate.yaml#L258) |
| `product.models.profiles.deepseek-v4-flash.model` | `deepseek-ai/DeepSeek-V4-Flash` | [YAML](../../config/workmate.yaml#L260) |
| `product.models.profiles.deepseek-v4-flash.base_url_env` | `CHAT_MODEL_BASE_URL` | [YAML](../../config/workmate.yaml#L263) |
| `product.models.profiles.deepseek-v4-flash.api_key_env` | `CHAT_MODEL_API_KEY` | [YAML](../../config/workmate.yaml#L265) |
| `product.models.profiles.deepseek-v4-flash.media_profile` | `text` | [YAML](../../config/workmate.yaml#L267) |
| `product.models.profiles.deepseek-v4-flash.supports_thinking` | `false` | [YAML](../../config/workmate.yaml#L271) |
| `product.models.profiles.deepseek-v4-flash.context_size` | `1024000` | [YAML](../../config/workmate.yaml#L273) |
| `product.models.profiles.deepseek-v4-flash.max_output_tokens` | `8192` | [YAML](../../config/workmate.yaml#L275) |
| `product.models.profiles.kimi-2.6.type` | `openai` | [YAML](../../config/workmate.yaml#L278) |
| `product.models.profiles.kimi-2.6.model` | `Pro/moonshotai/Kimi-K2.6` | [YAML](../../config/workmate.yaml#L280) |
| `product.models.profiles.kimi-2.6.base_url_env` | `CHAT_MODEL_BASE_URL` | [YAML](../../config/workmate.yaml#L283) |
| `product.models.profiles.kimi-2.6.api_key_env` | `CHAT_MODEL_API_KEY` | [YAML](../../config/workmate.yaml#L285) |
| `product.models.profiles.kimi-2.6.media_profile` | `vision-documents` | [YAML](../../config/workmate.yaml#L288) |
| `product.models.profiles.kimi-2.6.supports_thinking` | `true` | [YAML](../../config/workmate.yaml#L292) |
| `product.models.profiles.kimi-2.6.context_size` | `256000` | [YAML](../../config/workmate.yaml#L294) |
| `product.models.profiles.kimi-2.6.max_output_tokens` | `8192` | [YAML](../../config/workmate.yaml#L296) |
| `product.models.embedding.type` | `openai` | [YAML](../../config/workmate.yaml#L300) |
| `product.models.embedding.model` | `BAAI/bge-m3` | [YAML](../../config/workmate.yaml#L302) |
| `product.models.embedding.dimensions` | `1024` | [YAML](../../config/workmate.yaml#L304) |
| `product.models.embedding.base_url_env` | `EMBEDDING_MODEL_BASE_URL` | [YAML](../../config/workmate.yaml#L306) |
| `product.models.embedding.api_key_env` | `EMBEDDING_MODEL_API_KEY` | [YAML](../../config/workmate.yaml#L308) |
| `workmate_runtime.workspace_lifecycle.warm_ttl_seconds` | `600` | [YAML](../../config/workmate.yaml#L314) |
| `workmate_runtime.workspace_lifecycle.sweep_interval_seconds` | `60` | [YAML](../../config/workmate.yaml#L316) |
| `workmate_runtime.workspace_lifecycle.max_warm_per_worker` | `16` | [YAML](../../config/workmate.yaml#L319) |
| `workmate_runtime.sandbox.provider` | `bubblewrap` | [YAML](../../config/workmate.yaml#L324) |
| `workmate_runtime.worker.capacity` | `4` | [YAML](../../config/workmate.yaml#L351) |
| `workmate_runtime.worker.lease_seconds` | `30` | [YAML](../../config/workmate.yaml#L353) |
| `workmate_runtime.worker.drain_seconds` | `30` | [YAML](../../config/workmate.yaml#L355) |
| `workmate_runtime.worker.startup_timeout_seconds` | `60` | [YAML](../../config/workmate.yaml#L357) |
| `workmate_runtime.memory.enabled` | `true` | [YAML](../../config/workmate.yaml#L366) |
| `workmate_runtime.memory.max_instances` | `256` | [YAML](../../config/workmate.yaml#L368) |
| `workmate_runtime.memory.fallback` | `bm25` | [YAML](../../config/workmate.yaml#L370) |
| `workmate_runtime.skills.storage.timeout_seconds` | `30` | [YAML](../../config/workmate.yaml#L376) |
| `workmate_runtime.skills.storage.region` | `us-east-1` | [YAML](../../config/workmate.yaml#L378) |
| `workmate_runtime.skills.discovery_mode` | `explicit` | [YAML](../../config/workmate.yaml#L380) |
| `workmate_runtime.skills.skillhub_cn.enabled` | `false` | [YAML](../../config/workmate.yaml#L383) |
| `workmate_runtime.skills.skillhub_cn.base_url` | `https://api.skillhub.cn` | [YAML](../../config/workmate.yaml#L385) |
| `workmate_runtime.skills.skillhub_cn.request_timeout_secs` | `30` | [YAML](../../config/workmate.yaml#L387) |
| `workmate_runtime.skills.skillhub_cn.max_retries` | `2` | [YAML](../../config/workmate.yaml#L389) |
| `workmate_runtime.mcps.github.enabled` | `false` | [YAML](../../config/workmate.yaml#L394) |
| `workmate_runtime.artifacts.delivery_mode` | `hybrid` | [YAML](../../config/workmate.yaml#L398) |
| `workmate_runtime.artifacts.strict_delivery` | `false` | [YAML](../../config/workmate.yaml#L400) |
| `command_queue.poll_interval_seconds` | `0.2` | [YAML](../../config/workmate.yaml#L406) |
| `command_queue.batch_size` | `64` | [YAML](../../config/workmate.yaml#L408) |
| `command_queue.lease_seconds` | `30` | [YAML](../../config/workmate.yaml#L410) |
| `command_queue.repair_interval_seconds` | `30` | [YAML](../../config/workmate.yaml#L412) |
| `command_queue.redelivery_after_seconds` | `120` | [YAML](../../config/workmate.yaml#L414) |
| `command_queue.max_attempts` | `20` | [YAML](../../config/workmate.yaml#L416) |
| `command_queue.retry_max_seconds` | `60` | [YAML](../../config/workmate.yaml#L418) |
| `command_queue.pending_idle_seconds` | `30` | [YAML](../../config/workmate.yaml#L420) |
| `command_queue.retention_seconds` | `86400` | [YAML](../../config/workmate.yaml#L422) |
| `realtime.retention_seconds` | `3600` | [YAML](../../config/workmate.yaml#L427) |
| `realtime.max_log_bytes` | `8388608` | [YAML](../../config/workmate.yaml#L429) |
| `realtime.ttl_seconds` | `7200` | [YAML](../../config/workmate.yaml#L431) |
| `realtime.snapshot_interval_seconds` | `5.0` | [YAML](../../config/workmate.yaml#L433) |
| `realtime.snapshot_max_bytes` | `65536` | [YAML](../../config/workmate.yaml#L435) |
| `realtime.max_buffer_bytes` | `2097152` | [YAML](../../config/workmate.yaml#L437) |
| `realtime.max_buffer_events` | `256` | [YAML](../../config/workmate.yaml#L439) |
| `realtime.operation_timeout_seconds` | `2.0` | [YAML](../../config/workmate.yaml#L441) |
| `realtime.outage_grace_seconds` | `30.0` | [YAML](../../config/workmate.yaml#L443) |
| `realtime.reader_page_events` | `100` | [YAML](../../config/workmate.yaml#L445) |
| `realtime.client_buffer_events` | `256` | [YAML](../../config/workmate.yaml#L447) |
| `realtime.client_buffer_bytes` | `2097152` | [YAML](../../config/workmate.yaml#L449) |
| `realtime.reader_block_ms` | `1000` | [YAML](../../config/workmate.yaml#L451) |
| `realtime.reset_max_rows` | `10000` | [YAML](../../config/workmate.yaml#L453) |
| `realtime.reset_max_bytes` | `8388608` | [YAML](../../config/workmate.yaml#L455) |
| `realtime.authorize_interval_seconds` | `15.0` | [YAML](../../config/workmate.yaml#L457) |
| `realtime.heartbeat_interval_seconds` | `15.0` | [YAML](../../config/workmate.yaml#L459) |
| `realtime.reset_retry_seconds` | `2.0` | [YAML](../../config/workmate.yaml#L461) |
| `realtime.max_gateway_readers` | `1024` | [YAML](../../config/workmate.yaml#L463) |
| `realtime.max_gateway_subscriptions` | `4096` | [YAML](../../config/workmate.yaml#L465) |

## Deployment environment variables

See [.env.example](../../.env.example) for the template and adjacent comments. Only names are listed here; secrets and environment values are not copied. Default Compose passes only explicitly listed variables.

- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `PIP_INDEX_URL`
- `PIP_DEFAULT_TIMEOUT`
- `COMPOSE_PROJECT_NAME`
- `PRODUCT_API_URL`
- `WORKMATE_PUBLIC_URL`
- `DATABASE_URL`
- `WORKMATE_ALEMBIC_CONFIG`
- `RUNTIME_BASE_URL`
- `JWT_SECRET`
- `WORKMATE_INTERNAL_TOKEN`
- `ARTIFACT_STORAGE_DIR`
- `SKILL_S3_ENDPOINT`
- `SKILL_S3_BUCKET`
- `SKILL_S3_ACCESS_KEY`
- `SKILL_S3_SECRET_KEY`
- `WORKMATE_SKILL_CACHE_DIR`
- `AS_DATABASE_URL`
- `AS_REDIS_URL`
- `WORKMATE_COORDINATION_NAMESPACE`
- `WORKMATE_COORDINATION_REDIS_URL`
- `WEB_SEARCH_BASE_URL`
- `WEB_FETCH_BASE_URL`
- `SEARXNG_SECRET`
- `WEB_FETCH_API_TOKEN`
- `GITHUB_TOKEN`
- `CHAT_MODEL_API_KEY`
- `EMBEDDING_MODEL_API_KEY`
- `CHAT_MODEL_BASE_URL`
- `EMBEDDING_MODEL_BASE_URL`
- `KNOWLEDGE_QDRANT_URL`
- `KNOWLEDGE_QDRANT_API_KEY`
- `KNOWLEDGE_S3_ENDPOINT`
- `KNOWLEDGE_S3_BUCKET`
- `KNOWLEDGE_S3_ACCESS_KEY`
- `KNOWLEDGE_S3_SECRET_KEY`
- `KNOWLEDGE_EMBEDDING_MODEL_API_KEY`
- `KNOWLEDGE_EMBEDDING_MODEL_BASE_URL`
- `WORKMATE_WORKER_STORAGE_NODE_ID`
- `DOCKER_HOST`
- `WORKMATE_SANDBOX_KUBECONFIG`
- `E2B_API_KEY`
- `E2B_DOMAIN`
- `DAYTONA_API_KEY`
- `DAYTONA_API_URL`
- `OPEN_SANDBOX_API_KEY`
- `OPEN_SANDBOX_DOMAIN`
- `WORKMATE_WORKER_WORKSPACE_BASEDIR`
- `WORKMATE_WORKER_REME_BASEDIR`
- `WORKMATE_WORKER_STOP_GRACE_PERIOD`
- `WORKMATE_WORKER_NODES`
- `ARTIFACT_S3_ENDPOINT`
- `ARTIFACT_S3_BUCKET`
- `ARTIFACT_S3_ACCESS_KEY`
- `ARTIFACT_S3_SECRET_KEY`
