# 部署与运维

[English](../en/deployment.md) | [简体中文](../zh-CN/deployment.md)

从单机工作空间到多 Worker 部署，本指南涵盖服务组成、数据持久化、扩容、更新和备份。

## 服务与隔离

默认部署包括 `gateway`、`runtime`、`frontend`、PostgreSQL、Redis、RustFS 和一次性 `storage-init`。知识库启用 `knowledge` profile，Web 启用 `web-tool` profile。镜像分别为 `workmate-gateway`、`workmate-runtime`、`workmate-frontend`。

使用仓库根目录的 Compose 从源码构建；安装步骤见 [快速开始](getting-started.md)。默认 Bubblewrap 需要 Linux 命名空间，当前 Runtime 容器配置包含 `SYS_ADMIN` 和放宽的 seccomp/AppArmor。应评估宿主机隔离和部署信任边界，不把这些权限视为任意环境都安全的默认值。

内部服务地址 `http://gateway:8080`、`http://runtime:8000` 仅在相应容器网络内有效。数据库、Redis、对象存储和内部 Runtime 接口不应公开。公网入口使用 TLS 反向代理并正确支持 SSE，不缓存流式响应，配置适当的代理超时。

## 数据持久化

保留 PostgreSQL、Redis AOF、RustFS、产品文件、Worker Workspace 与 ReMe 卷。Skill 缓存可以重建，但不能把所有 Worker 数据都当缓存删除。

Compose 项目名决定卷与网络命名；已有部署改变项目名会指向另一组资源。部署更新时保持项目名与数据卷身份。不要使用 `docker compose down -v` 作为更新命令。

## 更新

1. 备份数据库、对象数据及配置，记录当前镜像和有效配置。
2. 停止新任务准入，等待执行与审批任务进入可安全维护状态，再停止写入角色。
3. 使用匹配版本构建 Gateway、Runtime、Frontend，并先完成数据库迁移。
4. 验证 Gateway、对象存储与初始化作业，再启动 Worker 和前端。
5. 检查登录、普通对话、审批、停止、成果下载及必要的集成。

新安装使用空的 PostgreSQL 数据库或模式（schema），由 `0001_initial_schema` 创建表结构。初始化会拒绝非空模式，且不支持通过降级删除已初始化的数据。迁移版本标记（stamp）不能替代实际迁移。

Gateway 容器入口执行迁移。源码启动时，在 `backend/` 配置连接后先执行 `.venv/bin/alembic upgrade head`。不能仅更新前端或通过 stamp 跳过实际迁移。数据库前向升级后，回退镜像不一定等于可回退系统。

## 多 Worker 与多机器

Workmate 支持 Gateway 与 Worker 分离部署、多 Worker 副本以及固定多节点执行。可以从单机 Compose 起步，随着团队并发需求增加，扩展 Worker 资源并规划存储节点；不同会话可并发处理，同一会话仍串行执行。

同节点副本只有在访问同一持久工作集并使用一致存储身份时才能交接。扩容不会自动产生可共享的 Workspace。先核对沙箱后端、目录挂载、所有权与暂停恢复能力，再增加副本。

固定多节点部署要求成果与附件使用 S3，各真实存储节点拥有唯一 `WORKMATE_WORKER_STORAGE_NODE_ID`。Gateway 配置 `WORKMATE_WORKER_NODES`，各角色共享数据库、Redis 与对象存储，内部地址必须跨主机可达。同一用户与 Agent 的存储归属保持固定。

原节点离线不触发任意工具的自动重放或透明接管。显式检查点恢复见 [任务与恢复](runtime.md)。部署多 Gateway 或多机器时，请根据实际负载验证并发容量和故障处理，并配置相应监控。

## 备份与排障

一致性冷备份要求在整个备份窗口停止全部写入方，并覆盖数据库与对应文件/对象数据。先在隔离资源中验证恢复，不能直接覆盖唯一可用数据。不要将备份、日志和配置中的密钥上传公开仓库。

排障优先检查 Compose 状态、Gateway/Worker 健康、迁移、数据库连接、Redis 空间与对象存储权限。Redis 使用 AOF 和 `noeviction`；内存满会拒绝写入，需监控积压和磁盘，而不是淘汰未确认命令。

通用工具与参数说明见 [测试与运维工具](tooling.md)。远程操作必须显式指定目标；操作前阅读 `--help` 并优先使用只读预览。已有部署须保留原 Compose 项目名，新安装默认使用 `workmate`。
