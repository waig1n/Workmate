---
name: skill-creator
display_name: 技能创建助手
description: 创建或改进 Workmate 技能，明确触发场景、验证真实效果，并通过平台审批入库；不自动绑定智能体。
version: "3"
tags: [Workmate, skill, creation]
license: Complete terms in LICENSE.txt
---

<!-- Modified for Workmate; see NOTICE and LICENSE.txt. -->


# 创建与改进技能

帮助用户把重复工作方法变成可复用技能，按“明确需求 → 草稿 → 验证 → 改进 → 审批入库”完成工作。使用用户的语言，保留现有授权和平台注入的创建澄清要求。

## 1. 明确需求与验收

先从对话提取目标、使用者、输入、输出、约束和例子。只询问会改变实现的缺失信息；平台要求等待澄清时先等待。列出一条应触发的请求、一条相近但不应触发的请求，以及可观察的成功标准。

改进已有技能时先读取真实内容与用户反馈，保留名称和许可证，明确哪些行为应保留。不要为了加功能扩大触发范围；简单问题不应被技能接管为复杂工作流。

## 2. 编写草稿

在当前会话的 `working/<skill-name>/` 中创建或复制草稿，不直接改已安装目录。
`input/` 是用户材料，`working/` 放草稿和评测中间文件，`output/` 放最终交付物。
`$SKILL_DIR` 指当前已加载本技能的真实父目录，需从工具提供的路径确定，不猜固定挂载路径。

- YAML frontmatter 必须含与目录一致的 name（小写字母、数字、连字符）和非空 description。描述要解释“做什么、何时使用”，而非堆关键词。
- SKILL.md 保留关键决策、顺序、输入输出和失败处理；大量细节放 `references/`，在正文说明何时读取。
- 只有重复、确定性工作才写 `scripts/`；输出模板放 `assets/`。不添加无用途的占位文件或不支持的工具配置。
- 现有项目优先遵守其约定；技能不是权限授权。不要虚构工具、API、凭据、子智能体或全局安装机制。

可用 `python "$SKILL_DIR/scripts/init_skill.py" --help` 查看初始化方式；初始化后清除示例占位。多步骤/条件流程参考 `references/workflows.md`；格式模板参考 `references/output-patterns.md`，按需阅读。

## 3. 验证、反馈与迭代

1. **静态检查**：名称、描述、相对引用、资源完整性、依赖、工作区路径、网络和其他副作用。可运行 `scripts/quick_validate.py` 做格式检查，但它不是安全扫描，也不替代平台校验。不要为安装而执行不可信包中的脚本。
2. **代表性验证**：选择正向请求、相邻负例和一个失败边界。对自己编写或已审查的确定性脚本运行实际检查，读取真实产物；缺依赖或工具时记录阻塞，不默默装包或绕过限制。
3. **区分证据**：人工走查、脚本测试、独立模型触发评测不是同一件事。没有隔离模型/子智能体能力时顺序走查，明确证据限制，不编造 baseline、Token、耗时或扫描结果。
4. **迭代**：基于问题定位最小修改，重新执行受影响的检查。已有版本比较时把基线保存在 `working/`，使用相同输入和验收标准，不让基线覆盖用户文件。

需要详细评估时阅读 [评测流程](references/workmate-evaluation.md)。`agents/grader.md`、`agents/comparator.md` 和 `agents/analyzer.md` 是评分/对比/分析参考，不会自动创建 Agent。复杂任务才使用这些材料，不对所有小技能强制跑完整基准。

## 4. 通过平台入库

1. 调用 `skill_validate` 校验 `working/` 下的技能目录，保留返回的 SHA-256 摘要。
2. 展示可审核的提交摘要：名称、简介、用途、组织统一技能库、文件内容与重要依赖。不把 Agent 绑定混入安装摘要。
3. 调用 `skill_install`，传入相同目录、expected_sha256；无需选择部门或传 scope_id，等待平台强制审批。Full Access 或对话中的同意不能代替审批卡，`skill.manage` 仍由平台检查。
4. 草稿发生变化则重新校验、重新审批。审批拒绝后停止；只有用户重新提出要求才再次提交。
5. 以工具成功结果为准报告技能记录 ID；草稿、格式检查通过或 ZIP 已生成都不代表安装完成。

平台工具缺失时，可使用 `python "$SKILL_DIR/scripts/package_skill.py" working/<skill-name> output/` 交付 ZIP，指导有权限的用户上传。打包不发布；不通过 Shell API、复制运行时目录或全局 CLI 绕过安装审批。最终文件使用 `present_files`（若提供），遵循实际 schema 和返回链接。

安装技能不会自动绑定任何 Agent。用户另有绑定需求时先完成入库，由组织技能管理员共享到使用部门，再通过独立 `agent_update` 流程和审批编辑 Agent；分别报告结果，绑定失败不回滚成功安装。
