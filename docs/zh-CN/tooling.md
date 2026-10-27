# 测试与运维工具

[English](../en/tooling.md) | [简体中文](../zh-CN/tooling.md)

仓库提供部署、回归测试、负载测试、故障演练和发布工具。根据目标环境选择工具，先预览操作，再执行会创建或修改资源的命令。

## 目录与职责

| 目录/入口 | 用途 |
|---|---|
| `backend/tests`、`runtime/tests`、`packages/product/tests` | 产品、执行协议与共享业务回归 |
| `frontend/e2e` | 浏览器功能与截图回归，使用通用示例数据 |
| `scripts/tests` | 运维工具的参数、安全边界与编排单元测试 |
| `scripts/reliability/load.py` | 本地隔离环境中的确定性混合负载 |
| `scripts/reliability/boundary_drill.py`、`database_tool_drill.py` | 执行边界崩溃、工具运行期间数据库故障 |
| `scripts/reliability/dependency_drill.py`、`s3_http_fault_drill.py` | 依赖和对象存储故障注入 |
| `scripts/reliability/capacity_support`、`soak_support` | 容量与持续运行客户端、状态及流式一致性检查 |
| `scripts/reliability/remote_fixture.py` | 显式目标上的隔离测试资源创建、状态检查、采集与清理 |
| `scripts/reliability/cold_backup.py`、`restore_drill.py` | 冷备份与隔离恢复验证 |
| `scripts/release` | 构建、镜像审计、打包与发布工具 |

数据库初始化使用 `0001_initial_schema`；后续数据库迁移和 Core 状态格式的兼容性由相应回归测试检查。

## 本地测试

安装开发依赖后，在仓库根目录运行。工具测试使用 Runtime 虚拟环境，避免与后端同名 `scripts` 包冲突：

```bash
runtime/.venv/bin/python -m pytest scripts/tests -q --noconftest
runtime/.venv/bin/python -m scripts.reliability.load --runtime-image workmate-runtime:2.0 --gateway-image workmate-gateway:2.0 --workload mixed
```

第二条命令只输出计划；加 `--run` 才创建隔离 Docker 资源。需要预先构建同架构镜像、Linux namespace 和所需容器权限。缺少外部服务而跳过的测试不计入真实集成验证。

`baseline.py` 默认只采样本机；指定 `--remote user@host` 才读取远端。基线健康检查使用目标机器的 `http://127.0.0.1:8080/health`。容量和持续运行工具的具体负载参数通过 `--help` 查看。

## 远程测试

```bash
runtime/.venv/bin/python -m scripts.reliability.remote_fixture provision --remote operator@example.test --runtime-image workmate-runtime:2.0 --gateway-image workmate-gateway:2.0 --output /tmp/workmate-capacity
```

替换示例主机，先检查计划，再加 `--run` 执行。Runtime 与基础镜像必须存在于目标 Docker，Gateway 依赖镜像必须存在于本机；工具解析镜像 ID 后执行。SSH 必须可用，远端操作需要非交互 sudo/Docker 权限。

`--remote` 可由明确设置的 `WORKMATE_TEST_SSH_TARGET` 代替，没有内置目标主机。状态文件记录主机和测试资源归属；后续 `status`、`collect`、`cleanup` 使用 `--state` 和相同目标。容量/持续运行流程从该状态获取目标并传给监控子进程。

清理操作只处理已确认归属的测试资源，保留现有业务容器。报告和日志存放在独立的输出目录；远程测试结果应注明目标环境、镜像版本与负载配置。

## 部署与依赖维护

```bash
./scripts/workmate check
./scripts/workmate up --dry-run
./deploy/start.sh
```

部署入口统一使用根目录 `.env.example`、`config/workmate.yaml` 和 `docker-compose.yml`；`deploy/start.sh` 调用同一部署工具，根据配置选择服务。首次运行前完成 [快速开始](getting-started.md)。新安装默认项目名为 `workmate`，已有部署必须显式保留原项目名和数据卷。

本机 `docker-compose.override.yml`、镜像版本覆盖及生成的工作材料不纳入版本控制。需要覆盖时由部署方自行维护。

```bash
python3 scripts/sync_images_to_remote.py --remote operator@example.test --check
python3 scripts/runtime_install.py --target local --project workmate --apt poppler-utils
python3 scripts/runtime_install.py --target remote --remote operator@example.test --project workmate --apt poppler-utils
python3 scripts/sync_images_to_remote.py cleanup --target remote --remote operator@example.test --compose-dir /srv/workmate
```

热安装和镜像清理默认预览，添加 `--apply` 后执行；镜像同步使用 `--check` 预览，省略该参数后执行。热安装只修改当前容器，重建后会丢失；持久依赖应进入 Runtime 镜像。请始终核对目标、项目名、资源标识与工具输出。

## 发布包

`python -m scripts.release --help` 列出 check、build、verify、bundle、publish 与 import 命令。Python 镜像包含应用源码、源码哈希，以及项目和第三方声明。验证工具检查已安装模块、运维入口、镜像各层和嵌套归档中的环境文件与已知私钥标记。发布前还需检查待发布源码中的敏感信息。

不提供验收报告的 `verify` 仅检查打包。`bundle` 与 `publish` 还要求干净的正式构建和与精确镜像 ID 绑定的业务验收证据。应用 SBOM 记录可获得的依赖许可信息，不涵盖操作系统包或独立提供的基础设施镜像。再次分发前应核对[第三方来源](third-party.md)。发布必须显式执行，不会因构建或验证而自动发生。
