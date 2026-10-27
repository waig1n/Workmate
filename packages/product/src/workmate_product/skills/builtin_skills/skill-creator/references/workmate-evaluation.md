<!-- Modified for Workmate; see NOTICE and LICENSE.txt. -->

# Workmate 技能效果验证

在技能行为复杂、需要稳定重复使用或用户要求评估时阅读。

## 记录真实证据

在 `working/<skill-name>-evaluation/` 下按用例保存输入、预期、真实产物和结果。
每项断言写清观察依据，不能仅记录“通过”。用例覆盖：

- 正向：该技能适用的真实请求，产物是否满足任务要求。
- 负向：相似但不适用的请求，是否错误扩展任务。
- 边界：输入缺失、格式错误、工具不可用或权限拒绝，是否明确停止/降级。

脚本测试只能证明其实际覆盖的行为；触发评测需要真实且隔离的模型执行。若当前工具
不支持后者，标记“未进行触发评测”，人工走查不填充虚假的成功率。

## 按需评分与对比

结构化记录参考 `schemas.md`；使用 `../agents/grader.md` 判断断言、
`../agents/comparator.md` 对比同一输入下的产物，`../agents/analyzer.md` 分析失败原因。
这些是参考方法，实际工具权限和可用性决定能执行到哪里。
缺失 Token、耗时等字段标记未记录，不用估计值冒充测量。

如已有真实评分文件，可用 `scripts/aggregate_benchmark.py --help` 查看汇总方式，
脚本路径相对当前 Skill 根目录。只有实际执行的基线才可用于回归比较。

## 离线评阅

从 Skill 根目录定位脚本，但保持执行工作区为会话目录：

```bash
python "$SKILL_DIR/eval-viewer/generate_review.py" working/<skill-name>-evaluation --static output/skill-review.html
```

此模式只生成静态文件；不会调用 Claude CLI、监听端口或终止进程。检查输出存在再交付。
没有独立评测环境时，直接给出用例、发现和证据限制即可，不强制生成报告。
