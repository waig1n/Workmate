# Third-party sources and licenses

[English](../en/third-party.md) | [简体中文](../zh-CN/third-party.md)

## Project license

Original Workmate code is licensed under [Apache-2.0](../../LICENSE). [NOTICE](../../NOTICE) records project attribution. Third-party material retains its own terms. The project license does not relicense dependencies, container images, fonts, or downloaded model assets.

## Bundled source

| Material | Source and applicable notices |
|---|---|
| MCP GitHub catalog | Adapted from [AgentScope](https://github.com/agentscope-ai/agentscope), `src/agentscope/app/hub/_mcp/_github_hub.py`, version 2.0.7. The full upstream [LICENSE.agentscope](../../packages/product/src/workmate_product/mcp/LICENSE.agentscope) is retained beside the implementation. |
| Skills obtained through DeerFlow | [DeerFlow public Skills](https://github.com/bytedance/deer-flow/tree/main/skills/public). Each derived Skill contains `LICENSE.deerflow` with the original MIT copyright notice and a `NOTICE` identifying Workmate adaptations. More specific upstream terms remain applicable. |
| frontend-design and skill-creator | [Anthropic Skills](https://github.com/anthropics/skills), obtained through DeerFlow. Existing Apache-2.0 `LICENSE.txt` files remain in each Skill. Modified source files are marked. |
| Chart reference specifications | [AntV chart-visualization-skills](https://github.com/antvis/chart-visualization-skills). `chart-visualization/LICENSE.antvis` preserves the MIT terms and AntV Visualization Team copyright. |

The [bundled Skill directories](../../packages/product/src/workmate_product/skills/builtin_skills) are the authoritative location for per-Skill notices. Keep these files in exported Skill ZIPs as well as application distributions. `agent-bootstrap` is Workmate-specific; it is not attributed to DeerFlow's bootstrap Skill.

Bundled Skills still require capability authorization and explicit installation/binding. Including a Skill in source does not authorize every user or bind it to every Agent.

## Dependencies and distributions

Python dependencies are recorded in `backend/uv.lock`, `runtime/uv.lock`, and exported `requirements.lock` files. Node dependencies are recorded in `frontend/package-lock.json`. Lockfiles record dependency versions. Distributions must also retain package license files and account for missing or nonstandard license metadata.

The release tool's application SBOM includes installed package names, versions, and available license metadata. Missing license metadata remains unknown. It excludes OS packages, fonts, and independently supplied infrastructure images; inventory those against the exact distributed image digests.

The default Compose file references PostgreSQL, Redis, RustFS, and optional Qdrant, SearXNG, and Crawl4AI services. Their licenses are independent of Workmate:

- `redis:7-alpine` is a floating tag. Redis 7.2 and earlier use BSD-3-Clause; Redis 7.4 uses RSALv2/SSPLv1. Resolve and review the actual version before redistribution; see [Redis licensing](https://redis.io/legal/licenses/).
- SearXNG uses AGPL; review its [license](https://github.com/searxng/searxng/blob/master/LICENSE) and any obligations for the distributed or modified service.
- Retain each other service's upstream license and bundled component notices. Runtime fonts and Debian/Alpine packages require their own inventory.

When distributing complete images or installation packages, review both application dependencies and infrastructure component licenses.
