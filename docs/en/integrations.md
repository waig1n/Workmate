# Skills, MCP, knowledge, and channels

[English](../en/integrations.md) | [简体中文](../zh-CN/integrations.md)

Connect specialized Skills, business systems, company knowledge, and messaging channels in one workspace. Platform permissions and approval policies govern access to these capabilities.

## Skills

Administrators upload ZIP packages containing `SKILL.md` into the organization library and configure sharing. Private object storage holds published packages, the database records immutable versions, and Workers unpack packages into local caches that can be rebuilt. Full Skill instructions are restricted to authorized managers.

Install required system and Python dependencies in the Runtime image. Temporary container installations disappear on recreation; installation inside an Agent sandbox is not host dependency management. Review third-party Skill licenses, origins, and execution risks separately, and retain original licenses.

## MCP

Managers with root `mcp.manage` permission can create custom HTTP connections, configure parameters, test connectivity, and set sharing. The server stores credential versions; ordinary members cannot read credentials. Sharing grants use of a managed connection without transferring the manager's platform identity to the Agent.

Connection tests check reachability. Task execution also checks caller permissions, Agent bindings, network access, and tool approval. Authentication depends on the connection configuration; a universal external OAuth flow for all MCP services is not currently available.

## Knowledge

1. Enable `product.knowledge.enabled` in YAML and configure a dedicated embedding profile.
2. Set `KNOWLEDGE_QDRANT_URL`, `KNOWLEDGE_QDRANT_API_KEY`, `KNOWLEDGE_S3_*`, and `KNOWLEDGE_EMBEDDING_MODEL_*`.
3. For local dependencies, run:

```bash
docker compose -f docker-compose.yml --profile knowledge up -d
```

4. After bucket initialization, create a knowledge base, upload documents, and start indexing in the management interface.
5. Grant retrieval access to target members or departments before binding the knowledge base to an Agent. Grant source download rights separately.

Documents become searchable after indexing completes. Evaluate results with representative business questions, then tune parsing, chunking, and embedding configuration for your documents.

## Web tools

The `web-tool` profile provides local Web dependencies. Set `SEARXNG_SECRET` and `WEB_FETCH_API_TOKEN`, and enable the corresponding tools in YAML:

```bash
docker compose -f docker-compose.yml --profile web-tool up -d
```

Search engines are configured in [settings.yml](../../config/searxng/settings.yml). Agent permissions, network-target checks, timeouts, and response-size limits still apply. External search links point to the destination website; artifacts delivered in the workspace use product-authorized downloads.

## IM channels

WeCom, Feishu, and DingTalk integrations are available. Personal WeChat iLink is experimental and disabled by default. Set a reachable `WORKMATE_PUBLIC_URL`; an administrator with `channel.manage` permission then configures robot credentials, a default Agent, and allowed groups before explicitly enabling the channel.

Manage robot credentials through the administration interface, not public YAML. Users must bind their platform identities. Group allowlists do not grant business-resource access. Tasks retain product identity, Agent permissions, and approval policy. Disabling a channel or unbinding an account does not delete historical Tasks.

Enterprise channels support delayed waiting notices and streaming updates. DingTalk streaming requires a published AI card template with correctly configured fields. Platform rate limits, message sizes, and feature differences may require segmented or fallback replies.

Scheduled notifications go only to the owner's bound private chat. Delivery failures are recorded and retried independently without rerunning the Task. Personal WeChat proactive delivery depends on usable recent private-chat context and is not guaranteed at arbitrary times. Validate connectivity, delivery, and platform permissions separately for each platform.
