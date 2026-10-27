# API usage

[English](../en/api.md) | [简体中文](../zh-CN/api.md)

Use the product API to connect Workmate to your applications, manage Agents, submit tasks, and retrieve results. API clients use the same authentication and authorization rules as the workspace.

## Endpoints and authentication

The product API prefix is `/api`. Gateway serves the installed OpenAPI schema at `/openapi.json` and interactive documentation at `/docs`, normally `http://localhost:8080/docs` locally. Use the deployed version's schema for request and response fields.

Obtain an access token from `/api/auth/login` and send `Authorization: Bearer <token>`. Keep tokens out of URLs, logs, and screenshots. SSE also uses fetch streaming with authorization headers, not URL tokens.

```bash
curl --fail --silent --show-error   -H "Authorization: Bearer ${WORKMATE_ACCESS_TOKEN}"   http://localhost:8080/api/auth/me
```

This variable is a local calling example, not a deployment setting. Supply the token through your application's credential-management mechanism.

## Authorization and concurrency

`GET /api/auth/me/permissions` provides an overview. A union of permission names across Scopes is not authorization for a specific target. Clients should use resource actions and scope information; the server checks each operation independently.

On version conflicts, reload the resource and review user intent before resubmitting. Do not automatically retry management writes, approval decisions, or unknown external side effects. Successful Task creation means the product accepted the request, not that tools have completed.

## Errors and stream recovery

Common HTTP statuses are 401 for authentication, 403 for permission denial, 404 for absent or invisible resources, 409 for state/version/binding conflicts, 422 for invalid input, 429 for capacity limits, and 503 for unavailable dependencies or nodes. Consult endpoint schemas and responses for specific error codes and fields.

Realtime events use dedicated cursors and a reset protocol. Business sequences and Redis cursors are not interchangeable. Reload an authorized server snapshot when outside the replay window; do not infer task state from the last text fragment alone.

## Endpoint directory

The directory below lists product endpoints in the current OpenAPI schema. Request bodies, permissions, and responses are defined by the installed version. Paths are relative to `/api`.


| Method | Path |
|---|---|
| `DELETE` | `/admin/permissions/{permission_id}` |
| `DELETE` | `/admin/roles/{role_id}` |
| `DELETE` | `/admin/scopes/{scope_id}` |
| `DELETE` | `/admin/scopes/{scope_id}/members/{user_id}` |
| `DELETE` | `/admin/scopes/{scope_id}/role-bindings/{user_id}/{role_key}` |
| `DELETE` | `/agents/{agent_id}` |
| `DELETE` | `/channels/bindings/{identity_id}` |
| `DELETE` | `/channels/{channel_id}` |
| `DELETE` | `/conversations/{conversation_id}` |
| `DELETE` | `/knowledge-bases/{kb_id}` |
| `DELETE` | `/knowledge-bases/{kb_id}/documents/{document_id}` |
| `DELETE` | `/mcps/{installation_id}` |
| `DELETE` | `/schedules/{schedule_id}` |
| `DELETE` | `/sharing/{kind}/{resource_id}/{target_scope_id}` |
| `DELETE` | `/skills/{installation_id}` |
| `GET` | `/admin/organization` |
| `GET` | `/admin/permissions` |
| `GET` | `/admin/roles` |
| `GET` | `/admin/roles/templates` |
| `GET` | `/admin/roles/{role_id}/bindings` |
| `GET` | `/admin/scopes/tree` |
| `GET` | `/admin/scopes/{scope_id}/assignable-roles` |
| `GET` | `/admin/scopes/{scope_id}/member-candidates` |
| `GET` | `/admin/scopes/{scope_id}/members` |
| `GET` | `/admin/scopes/{scope_id}/role-bindings` |
| `GET` | `/admin/security-audit-events` |
| `GET` | `/admin/users` |
| `GET` | `/admin/users/{user_id}` |
| `GET` | `/admin/users/{user_id}/effective-access` |
| `GET` | `/agents` |
| `GET` | `/agents/capability-candidates` |
| `GET` | `/agents/{agent_id}` |
| `GET` | `/artifacts` |
| `GET` | `/artifacts/{artifact_id}/download` |
| `GET` | `/auth/me` |
| `GET` | `/auth/me/permissions` |
| `GET` | `/auth/registration-status` |
| `GET` | `/channels` |
| `GET` | `/channels/bindings` |
| `GET` | `/channels/{channel_id}/deliveries` |
| `GET` | `/conversations` |
| `GET` | `/conversations/history` |
| `GET` | `/conversations/{conversation_id}` |
| `GET` | `/conversations/{conversation_id}/messages` |
| `GET` | `/conversations/{conversation_id}/recovery` |
| `GET` | `/knowledge-bases` |
| `GET` | `/knowledge-bases/{kb_id}` |
| `GET` | `/knowledge-bases/{kb_id}/documents` |
| `GET` | `/knowledge-bases/{kb_id}/documents/{document_id}/content` |
| `GET` | `/mcps` |
| `GET` | `/mcps/catalog` |
| `GET` | `/mcps/hubs` |
| `GET` | `/mcps/{installation_id}/configuration` |
| `GET` | `/models` |
| `GET` | `/schedules` |
| `GET` | `/schedules/notification-targets` |
| `GET` | `/schedules/{schedule_id}` |
| `GET` | `/schedules/{schedule_id}/occurrences` |
| `GET` | `/scopes/accessible` |
| `GET` | `/sharing/{kind}/{resource_id}` |
| `GET` | `/sharing/{kind}/{resource_id}/access` |
| `GET` | `/skills` |
| `GET` | `/skills/catalog` |
| `GET` | `/skills/hubs` |
| `GET` | `/skills/{installation_id}/description` |
| `GET` | `/tasks` |
| `GET` | `/tasks/conversations/{conversation_id}/events` |
| `GET` | `/tasks/{task_id}` |
| `GET` | `/tasks/{task_id}/events` |
| `GET` | `/tasks/{task_id}/knowledge-events` |
| `GET` | `/tasks/{task_id}/pending-confirm` |
| `GET` | `/tasks/{task_id}/tools/{tool_call_id}/content` |
| `GET` | `/usage` |
| `GET` | `/usage/options` |
| `GET` | `/usage/scopes` |
| `PATCH` | `/admin/organization` |
| `PATCH` | `/admin/permissions/{permission_id}` |
| `PATCH` | `/admin/roles/{role_id}` |
| `PATCH` | `/admin/scopes/{scope_id}` |
| `PATCH` | `/admin/users/{user_id}` |
| `PATCH` | `/agents/{agent_id}` |
| `PATCH` | `/auth/me` |
| `PATCH` | `/channels/{channel_id}` |
| `PATCH` | `/conversations/{conversation_id}` |
| `PATCH` | `/conversations/{conversation_id}/model` |
| `PATCH` | `/knowledge-bases/{kb_id}` |
| `PATCH` | `/mcps/{installation_id}` |
| `PATCH` | `/mcps/{installation_id}/custom` |
| `PATCH` | `/schedules/{schedule_id}` |
| `PATCH` | `/skills/{installation_id}` |
| `POST` | `/admin/permissions` |
| `POST` | `/admin/roles` |
| `POST` | `/admin/scopes` |
| `POST` | `/admin/scopes/{scope_id}/move` |
| `POST` | `/admin/scopes/{scope_id}/move-preview` |
| `POST` | `/admin/users` |
| `POST` | `/admin/users/{user_id}/disable` |
| `POST` | `/admin/users/{user_id}/enable` |
| `POST` | `/admin/users/{user_id}/reset-password` |
| `POST` | `/agents` |
| `POST` | `/agents/{agent_id}/restore-default-prompt` |
| `POST` | `/auth/login` |
| `POST` | `/auth/me/change-password` |
| `POST` | `/auth/register` |
| `POST` | `/channels` |
| `POST` | `/channels/deliveries/{delivery_id}/retry` |
| `POST` | `/channels/{channel_id}/binding-code` |
| `POST` | `/channels/{channel_id}/qr` |
| `POST` | `/channels/{channel_id}/qr-status` |
| `POST` | `/conversations` |
| `POST` | `/conversations/{conversation_id}/recovery` |
| `POST` | `/knowledge-bases` |
| `POST` | `/knowledge-bases/{kb_id}/documents` |
| `POST` | `/knowledge-bases/{kb_id}/documents/{document_id}/retry` |
| `POST` | `/knowledge-bases/{kb_id}/documents/{document_id}/start` |
| `POST` | `/knowledge-bases/{kb_id}/search` |
| `POST` | `/mcps` |
| `POST` | `/mcps/custom` |
| `POST` | `/mcps/probe` |
| `POST` | `/mcps/{installation_id}/probe` |
| `POST` | `/schedules` |
| `POST` | `/schedules/preview` |
| `POST` | `/schedules/{schedule_id}/occurrences/{occurrence_id}/notification/retry` |
| `POST` | `/schedules/{schedule_id}/pause` |
| `POST` | `/schedules/{schedule_id}/resume` |
| `POST` | `/sharing/{kind}/{resource_id}` |
| `POST` | `/sharing/{kind}/{resource_id}/batch` |
| `POST` | `/sharing/{kind}/{resource_id}/preview` |
| `POST` | `/skills` |
| `POST` | `/skills/upload` |
| `POST` | `/tasks/conversations/{conversation_id}/tasks` |
| `POST` | `/tasks/conversations/{conversation_id}/tasks-with-files` |
| `POST` | `/tasks/{task_id}/confirm` |
| `POST` | `/tasks/{task_id}/full-access` |
| `POST` | `/tasks/{task_id}/stop` |
| `PUT` | `/admin/roles/{role_id}/permissions` |
| `PUT` | `/admin/scopes/{scope_id}/members/{user_id}` |
| `PUT` | `/admin/scopes/{scope_id}/role-bindings/{user_id}/{role_key}` |
| `PUT` | `/admin/users/{user_id}/access` |
