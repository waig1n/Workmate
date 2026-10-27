# 快速开始

[English](../en/getting-started.md) | [简体中文](../zh-CN/getting-started.md)

按照本指南在 Linux 主机上构建 Workmate，创建团队工作空间并运行第一个任务。

## 环境要求

使用支持 Docker Compose v2 的 Linux 主机。默认 Bubblewrap 沙箱需要 Linux 命名空间支持；容器权限要求见 [部署运维](deployment.md)。准备一个与所选模型配置匹配的 API 凭据。首次构建需要访问容器镜像源和软件包源。

以下命令均在仓库根目录执行，显式指定 Compose 文件，以免自动加载机器上的 override 文件。

## 配置

```bash
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少完成：

1. 为 `POSTGRES_PASSWORD` 设置强密码，同步更新 `DATABASE_URL` 和 `AS_DATABASE_URL` 中的密码；URL 特殊字符需要编码。
2. 为 `JWT_SECRET` 和 `WORKMATE_INTERNAL_TOKEN` 分别生成随机值，不使用模板占位值。
3. 填写 `KNOWLEDGE_S3_ACCESS_KEY` / `KNOWLEDGE_S3_SECRET_KEY`，作为本地 RustFS 服务凭据；即使不启用知识库，Skill 存储也需要该服务。将 `SKILL_S3_ACCESS_KEY` / `SKILL_S3_SECRET_KEY` 设置为对应凭据，Skill 与知识库使用不同的桶。
4. 填写模型凭据及可选的网关地址。检查 `config/workmate.yaml` 中的聊天模型、向量模型和 ReMe 长期记忆配置，确保模型名称、类型、能力和环境变量引用与供应商相符。需要哪些模型见 [模型配置](models.md)。
5. 设置 `WORKMATE_PUBLIC_URL` 为用户实际访问的前端地址；本机体验可用 `http://localhost:3000`。

YAML 中已启用的可选功能也需要对应凭据和服务。知识库与 Web 的配置步骤见 [集成](integrations.md)。完整参数见 [配置说明](configuration.md)。

默认 YAML 配置已启用知识库和 Web 工具。下面的启动命令启用对应服务；请先设置 `KNOWLEDGE_QDRANT_API_KEY`、`KNOWLEDGE_EMBEDDING_MODEL_API_KEY`、`SEARXNG_SECRET` 与 `WEB_FETCH_API_TOKEN`。若只需要最小工作台，先将 `product.knowledge.enabled` 和 `product.web.enabled` 设为 `false`，再省略对应的 Compose 服务组（profile）。

## 构建与启动

```bash
docker compose -f docker-compose.yml build gateway runtime frontend
docker compose -f docker-compose.yml --profile knowledge --profile web-tool up -d
docker compose -f docker-compose.yml ps
```

默认入口：前端 `http://localhost:3000`，API 文档 `http://localhost:8080/docs`。实际端口以 Compose 有效配置为准。Runtime 不发布宿主机端口。

对象存储健康后，`storage-init` 初始化私有桶；Gateway 执行数据库迁移并就绪后，Worker 才启动。`storage-init` 是一次性作业，成功退出是正常状态。初始化或迁移失败时，请先按下方排障步骤处理。

## 首次使用

1. 打开前端，创建企业工作空间和首个管理员账号。
2. 使用管理员账号创建普通成员，按需配置部门和角色。
3. 选择默认工作台助手或创建个人智能体，选择可用模型；由管理员配置并共享需要的 Skill/MCP。
4. 新建对话并发送任务。需要确认时处理审批卡片，在“执行活动”中查看工具调用，在成果面板预览或下载输出。

后续操作见 [使用指南](user-guide.md) 和 [管理与安全](administration.md)。

## 启动失败

```bash
docker compose -f docker-compose.yml logs --tail=100 gateway runtime storage-init
```

先检查空白或不一致的凭据、模型名称、对象桶、迁移结果及沙箱权限。分享日志前移除认证头、密钥和用户内容。不要使用删除数据卷的方式修复初始化失败。
