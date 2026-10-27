# 配置说明

[English](../en/configuration.md) | [简体中文](../zh-CN/configuration.md)

按部署环境配置模型、存储和执行策略。日常配置集中在一个 YAML 文件和环境变量中，无需修改应用代码。

## 配置来源

不含密钥的功能与策略配置放在 [config/workmate.yaml](../../config/workmate.yaml)。连接、密钥和部署路径通过环境变量提供，部署变量模板见 [.env.example](../../.env.example)。`WORKMATE_CONFIG_FILE` 指定 YAML 路径。

设置加载优先级为显式初始化参数、非空环境变量/`.env`、YAML、代码默认值。默认 Compose 仅传入明确列出的变量；只把变量写入宿主机 `.env` 并不保证它进入容器。高级覆盖需要显式注入对应服务，不应把包含全部密钥的文件传给每个角色。

[参数清单](configuration-reference.md) 列出仓库 YAML 中所有配置项及部署变量名称。表中是示例文件的配置值，不等同于省略字段时的配置模型默认值。字段类型、枚举、范围与交叉约束以 [共享配置类型](../../packages/product/src/workmate_product/config) 和 [Gateway 配置](../../backend/app/config.py) 为准。

## 配置职责

| 范围 | 配置内容与边界 |
|---|---|
| 模型配置（Profile） | 模型名称、类型、能力、上下文及输出限制；密钥只通过 `api_key_env` 引用 |
| 认证与注册 | JWT 签名密钥在环境变量；注册模式、过期策略及限流按 YAML 配置 |
| Skill 存储 | `SKILL_S3_*` 提供 Gateway 与 Worker 共用的私有桶连接 |
| 成果与附件 | `product.artifacts.storage` 选择存储后端；S3 使用独立 `ARTIFACT_S3_*` 桶 |
| 知识库 | `product.knowledge.enabled` 开关；独立向量模型、Qdrant 和原文存储 |
| Worker | 存储节点身份、并发、租约、排空和 Workspace 生命周期 |
| 命令队列 | Redis 连接、命名空间、有限重试和保留时间；不替代 PG 执行权 |
| 实时事件 | 正文保留、写入缓冲、快照、读取器和客户端资源上限；不是集群总容量 |
| 沙箱 | 后端、资源回收与供应商参数；凭据只注入 Worker |
| 定时任务 / IM | 调度策略、渠道开关、账号环境变量引用及通知策略 |

## 连接与密钥

`DATABASE_URL` 用于产品数据库，`AS_DATABASE_URL` 用于 Runtime 数据库。两侧必须连接到部署对应的数据库。`WORKMATE_INTERNAL_TOKEN` 在 Compose 中映射为调用方和 Runtime 的内部接口凭据。

`WORKMATE_PUBLIC_URL` 是用户可访问的前端地址，支持 HTTP/HTTPS；公开部署建议 HTTPS。`PRODUCT_API_URL` 是 Next.js 服务端访问 Gateway 的地址，不能用它生成用户链接。

本地 RustFS 服务使用 `KNOWLEDGE_S3_*` 凭据启动。Skill 桶始终需要初始化；知识库与 Artifact 桶按启用功能初始化。外部 S3 由部署方预建私有桶并配置对应权限。不要在不同数据类型间复用桶。

`WORKMATE_WORKER_STORAGE_NODE_ID` 表示稳定存储挂载身份，不是临时进程 ID。`WORKMATE_WORKER_NODES` 用于固定节点目录；多机器配置必须结合 S3 与节点可达性，见 [部署运维](deployment.md)。

## 修改配置

修改前检查当前有效配置并备份。模型和策略变更可能需要重新创建相关服务才能加载；重新构建应用镜像不会自动更新已有数据库中的用户自定义 Agent。

如需新增配置项，请参阅[开发与贡献](development.md)。现有授权与审批规则始终由服务端执行。

## Redis、路径与后端连接

| 环境变量 | 用途 |
|---|---|
| `AS_REDIS_URL` | AgentScope 辅助组件使用的 Redis 连接 |
| `WORKMATE_COORDINATION_REDIS_URL`、`WORKMATE_COORDINATION_NAMESPACE` | 产品命令与实时协调连接和命名空间；同一部署各角色保持一致，不同部署使用不同命名空间 |
| `ARTIFACT_STORAGE_DIR` | 本地成果存储路径；选择本地后端时需要持久化并保证访问角色可见 |
| `WORKMATE_WORKER_WORKSPACE_BASEDIR`、`WORKMATE_WORKER_REME_BASEDIR` | Worker 工作文件与长期记忆目录，应挂载持久卷 |
| `WORKMATE_SKILL_CACHE_DIR` | Skill 解包缓存位置，不代替私有发布包桶 |
| `WORKMATE_WORKER_STOP_GRACE_PERIOD` | Compose 停机宽限，应覆盖 Worker 排空和资源清理 |
| `DOCKER_HOST` | Docker 沙箱连接；宿主 socket 挂载必须显式配置 |
| `WORKMATE_SANDBOX_KUBECONFIG` | Kubernetes 客户端配置；空值使用集群内身份，文件需显式只读挂载 |
| `E2B_API_KEY`、`E2B_DOMAIN` | E2B 凭据和可选域名，只供 Worker 使用 |
| `DAYTONA_API_KEY`、`DAYTONA_API_URL` | Daytona 凭据和可选服务地址 |
| `OPEN_SANDBOX_API_KEY`、`OPEN_SANDBOX_DOMAIN` | OpenSandbox 凭据和服务端点 |
| `WEB_SEARCH_BASE_URL`、`WEB_FETCH_BASE_URL` | Worker 使用的 Web 服务地址，不是用户浏览器地址 |
| `PIP_INDEX_URL`、`PIP_DEFAULT_TIMEOUT` | 构建时的软件包源与下载超时，不决定模型调用超时 |

命名空间、目录和节点身份关联已有数据。调整这些设置前，请规划数据迁移，并核对命令、文件引用、工作目录及执行归属。所有后端凭据仅传入实际使用它的角色，不进入 Agent 工具环境。
