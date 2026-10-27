# API 使用

[English](../en/api.md) | [简体中文](../zh-CN/api.md)

通过产品 API 将 Workmate 接入现有应用，管理智能体、提交任务并读取执行结果。API 与工作台使用相同的身份认证和权限规则。

## 入口与认证

产品 API 前缀为 `/api`。当前部署的完整 OpenAPI 定义 位于 Gateway 的 `/openapi.json`，交互文档位于 `/docs`；本机默认地址为 `http://localhost:8080/docs`。以实际部署版本的 接口定义为请求与响应字段依据。

使用 `/api/auth/login` 取得访问令牌，并发送 `Authorization: Bearer <token>`。不要把令牌放进 URL、日志或截图。SSE 使用携带认证头的 `fetch` 流式请求。

```bash
curl --fail --silent --show-error   -H "Authorization: Bearer ${WORKMATE_ACCESS_TOKEN}"   http://localhost:8080/api/auth/me
```

该变量仅为本地调用示例，不是部署配置项。应用应按自己的凭据管理方式提供令牌。

## 权限与并发

`GET /api/auth/me/permissions` 提供权限概览；某个范围内拥有的权限不代表对所有范围都有效。管理界面和客户端应使用具体资源动作与范围信息，服务端每次操作仍会校验。

遇到版本冲突时重新读取资源，核对用户意图后再提交。不要自动重试管理写操作、审批决定或未知外部副作用。创建 Task 成功表示产品接受执行请求，不表示工具已完成。

## 错误与流式恢复

常见 HTTP 状态：401 认证失败，403 无权限，404 不存在或不可见，409 状态/版本/绑定冲突，422 输入不合法，429 容量限制，503 依赖或节点不可用。具体错误码与字段以接口定义和实际响应为准。

实时事件使用专门游标与 reset 协议，不能混用业务序列和 Redis 游标。超出回放窗口时重新加载服务端授权快照；不要仅按最后一段文本猜测任务状态。

## 接口目录

下方列出当前 OpenAPI 中的产品接口。请求体、权限和响应以运行版本 OpenAPI 为准，路径均相对于 `/api`。


| 方法 | 路径 |
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
