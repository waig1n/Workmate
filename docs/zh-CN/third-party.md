# 第三方来源与许可

[English](../en/third-party.md) | [简体中文](../zh-CN/third-party.md)

## 项目许可

Workmate 自有代码采用 [Apache-2.0](../../LICENSE)，项目署名见 [NOTICE](../../NOTICE)。第三方材料保留各自许可；项目许可不改变依赖、容器镜像、字体或下载模型资产的许可。

## 随仓库分发的源码

| 内容 | 来源与适用声明 |
|---|---|
| MCP GitHub 目录 | 改编自 [AgentScope](https://github.com/agentscope-ai/agentscope) 2.0.7 的 `src/agentscope/app/hub/_mcp/_github_hub.py`。实现旁保留完整上游 [LICENSE.agentscope](../../packages/product/src/workmate_product/mcp/LICENSE.agentscope)。 |
| 通过 DeerFlow 引入的 Skill | 来源为 [DeerFlow public Skills](https://github.com/bytedance/deer-flow/tree/main/skills/public)。各改编技能内包含保留原始 MIT 版权声明的 `LICENSE.deerflow`，以及说明 Workmate 适配的 `NOTICE`；更具体的上游许可继续适用。 |
| frontend-design 与 skill-creator | 来源为 [Anthropic Skills](https://github.com/anthropics/skills)，经 DeerFlow 引入。各目录保留原有 Apache-2.0 `LICENSE.txt`，修改文件带有适配标记。 |
| 图表参考规格 | 来源为 [AntV chart-visualization-skills](https://github.com/antvis/chart-visualization-skills)。`chart-visualization/LICENSE.antvis` 保留 MIT 条款及 AntV Visualization Team 署名。 |

各技能的具体声明以[内置 Skill 目录](../../packages/product/src/workmate_product/skills/builtin_skills)内文件为准。应用包与独立导出的 Skill ZIP 均需保留这些文件。`agent-bootstrap` 为 Workmate 专用技能，不归为 DeerFlow bootstrap 的改编。

内置技能仍需能力授权、安装与显式绑定；随源码提供不代表所有用户自动获得使用权，也不代表自动绑定所有 Agent。

## 依赖与分发包

Python 依赖记录在 `backend/uv.lock`、`runtime/uv.lock` 及导出的 `requirements.lock` 中；Node 依赖记录在 `frontend/package-lock.json` 中。锁文件记录依赖版本；分发时还需保留各软件包许可证，并核对缺失或非标准许可元数据。

发布工具生成的应用 SBOM 包含已安装包的名称、版本和可获得的许可元数据；缺失信息仍为未知。该清单不包括操作系统包、字体及独立提供的基础设施镜像，后者需要针对实际分发的镜像摘要另行核对。

默认 Compose 引用 PostgreSQL、Redis、RustFS，以及可选的 Qdrant、SearXNG、Crawl4AI。它们的许可独立于 Workmate：

- `redis:7-alpine` 是浮动标签。Redis 7.2 及更早版本采用 BSD-3-Clause，Redis 7.4 采用 RSALv2/SSPLv1；分发前应确认实际版本及适用条款，见 [Redis 许可说明](https://redis.io/legal/licenses/)。
- SearXNG 使用 AGPL；应核对其[许可证](https://github.com/searxng/searxng/blob/master/LICENSE)及对应服务分发、修改所产生的义务。
- 其余服务也需保留各自上游许可及组件声明；Runtime 字体和 Debian/Alpine 包需要独立盘点。

分发完整镜像或安装包时，请同时检查应用依赖与基础设施组件的许可。
