# Skill、MCP、知识库与渠道

[English](../en/integrations.md) | [简体中文](../zh-CN/integrations.md)

将专业技能、业务系统、企业知识和消息渠道接入同一工作空间。各类能力统一受平台权限与审批策略管理。

## Skill

管理员在组织库上传包含 `SKILL.md` 的 ZIP 并设置使用范围。发布包保存在私有对象存储，数据库记录固定版本，Worker 将发布包解包到本地缓存，缓存可重新生成。完整 Skill 说明仅向有管理权限的用户开放。

技能脚本需要的系统与 Python 依赖应安装在 Runtime 镜像中。容器内临时安装会在重建后丢失；沙箱内安装只影响相应执行环境，系统依赖应通过镜像维护。第三方 Skill 的许可、来源和执行风险需要分别核对，保留原许可证。

## MCP

具有组织根范围 `mcp.manage` 权限的管理员可创建自定义 HTTP 连接、配置参数、测试连接和设置共享范围。服务端保存凭据版本，普通成员不能读取凭据。共享授予的是受管连接使用权，不会把管理身份传给 Agent。

连接测试用于检查连通性。任务执行时还会检查调用者权限、智能体绑定及工具审批策略。认证方式取决于具体连接配置，当前不提供适用于所有 MCP 服务的通用外部 OAuth 流程。

## 知识库

1. 在 YAML 启用 `product.knowledge.enabled`，配置独立的向量模型（Embedding）配置。
2. 配置 `KNOWLEDGE_QDRANT_URL`、`KNOWLEDGE_QDRANT_API_KEY`、`KNOWLEDGE_S3_*` 和 `KNOWLEDGE_EMBEDDING_MODEL_*`。
3. 使用本地依赖时运行：

```bash
docker compose -f docker-compose.yml --profile knowledge up -d
```

4. 确认桶初始化完成后，通过管理界面创建知识库，上传文档并启动索引。
5. 为目标成员或部门授予检索权限，再绑定到 Agent。原文下载权独立授予。

文档上传后需完成索引才能检索。可使用真实业务问题检查结果，并根据文档类型调整解析、分块和向量模型配置。

## Web 工具

本地 Web 依赖由 `web-tool` profile 提供。配置 `SEARXNG_SECRET`、`WEB_FETCH_API_TOKEN`，并在 YAML 中启用对应工具：

```bash
docker compose -f docker-compose.yml --profile web-tool up -d
```

搜索引擎配置见 [settings.yml](../../config/searxng/settings.yml)。工具仍受 Agent 权限、网络目标检查、超时和响应大小限制。搜索结果中的外部链接由目标网站提供；工作台中的成果文件则通过平台授权下载。

## IM 渠道

支持企业微信、飞书、钉钉接入；个人微信 iLink 是默认关闭的试验功能。配置可访问的 `WORKMATE_PUBLIC_URL`，再由具有 `channel.manage` 权限的管理者配置机器人凭据、默认 Agent 和允许的群，并显式启用渠道。

机器人凭据通过管理入口维护，不写入公开 YAML。用户必须绑定平台身份；群允许名单不是业务资源授权。每次任务继续使用产品身份、Agent 权限和审批策略。停用渠道或解绑不会删除历史任务。

企业渠道可配置延迟等待提示和流式更新。钉钉流式回复需要平台发布的 AI 卡片模板及正确的字段配置。平台限流、消息大小和能力差异可能导致分段或降级回复。

定时任务只向本人绑定的私聊通知。发送失败单独记录和重试，不通过重跑任务补发。个人微信主动发送依赖可用的近期私聊上下文，不能保证任意时间推送。完成配置后，请分别发送测试消息，检查平台权限及消息送达情况。
