# 开发与贡献

[English](../en/development.md) | [简体中文](../zh-CN/development.md)

欢迎贡献功能、修复问题、改进文档或完善集成。以下步骤帮助你准备开发环境、定位代码并提交可验证的变更。

## 环境

Python 要求 3.12 或更新的兼容版本；使用 `uv` 安装 Python 依赖，使用 npm 与仓库锁文件安装前端依赖。运行服务还需配置 PostgreSQL、Redis 和对象存储。AgentScope 使用锁文件指定的发布版本。

在仓库根目录执行：

```bash
(cd backend && uv sync --extra dev)
(cd runtime && uv sync --extra dev)
(cd frontend && npm ci)
```

依赖升级请作为明确的变更提交，同时更新对应依赖声明和锁文件。

## 验证

```bash
backend/.venv/bin/python -m pytest backend/tests/test_document_governance.py -q --noconftest
(cd backend && .venv/bin/python -m pytest tests -q)
(cd runtime && .venv/bin/python -m pytest tests -q)
(cd frontend && npm test && npm run typecheck)
```

部分集成测试需要配置外部服务；未配置时会跳过。浏览器测试使用 Playwright，配置见 [frontend](../../frontend)。会创建服务或写入数据的测试应使用独立环境，提交时注明实际执行范围和跳过项。

## 修改入口

| 修改 | 主要位置与约束 |
|---|---|
| 产品 API | `backend/app`；服务端重查权限与目标 Scope |
| 业务与持久化 | `packages/product`；应用编排事务，Repository 管理 SQL/锁 |
| 执行与工具 | `runtime/workmate_runtime`；保留 owner/epoch、审批与清理证据 |
| 共享协议 | `packages/contracts`；同步生产者、消费者与契约测试 |
| 工作台 | `frontend`；只访问产品 API，保留 SSE reset 与授权下载 |
| 数据库 | `backend/alembic`；以 `0001_initial_schema` 初始化；发布后新增前向迁移 |
| 配置 | 配置模型、YAML 注释、环境模板、Compose 与双语文档同步 |

功能修改应测试成功、拒绝和失败路径；授权、并发、审批及恢复需要覆盖相应边界。涉及协议和持久化的变更，应验证实际状态、事件顺序和存储结果。

## 文档维护

英文首页是 `README.md`，中文首页是 `README.zh-CN.md`。`docs/en/` 与 `docs/zh-CN/` 文件名一致，顶部提供对应语言入口。两种语言描述相同功能、限制和配置；修改一侧时同步另一侧。

文档按读者任务组织：用户指南说明操作步骤，架构与开发指南说明实现契约。功能说明应与已实现行为一致；中文采用自然的中文表达，英文独立润色，保留相同的功能、配置和限制。

当前行为以代码、配置模型与测试为依据；依赖契约以锁定版本源码为依据。配置清单见 [配置说明](configuration.md)，公开文档目录见 [索引](README.md)。

## 提交贡献

提交前检查变更差异，只包含与目标相关的文件，并保留第三方许可证和署名。凭据、日志与数据库备份保留在本地。

提交说明应包含要解决的问题、变更后的行为和测试结果；界面变更可附截图。涉及配置项时，同步更新配置模型、YAML 英文注释、中英文参数说明，以及默认值、加载、覆盖和非法值测试；新增部署变量还需更新 `.env.example` 与 Compose。
