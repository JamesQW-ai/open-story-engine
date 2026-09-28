状态：必需上下文包闸门已实现并复审；核心定向 114 项、完整 API 520 项通过。此记录只确认工程边界，不代表真实模型正文质量验收。

后续进展：[历史来源选择的标注评估与修正](context-management-history-selection-evaluation-2026-09-27.md)。下文保留本阶段验证结果。

## 审查发现与修复

| 原问题 | 修复后的行为 |
| --- | --- |
| `formal_required` 下，可见性声明有效但 resolver 缺失时返回 `skipped`，可能继续生成 | 正式模式要求成功构建 bundle，缺模块即拒绝 |
| 普通 `ContextBundleError`、`ContextBudgetError` 仅进入审计，调用端只检查另一种拒绝标记 | 统一检查 bundle 是否成功；必需包构建失败拒绝，已发生的受保护预算超限在兼容模式也拒绝 |
| 可选模式的 preflight 提前返回，复用 planner 时可能仍持有旧 bundle | 每次预检先清空上次 bundle、模块上下文和拒绝状态；失败不复用旧包 |
| 正文阶段另有一套错误处理，可能与写前规划的判断不同 | 正文入口复用相同预检与拒绝逻辑，保留规划后依据候选状态重新构建快照的顺序 |

API 的真实模型运行时固定构造 `require_context_bundle=True`。这个内部参数不提供环境变量开关，单次 context 的可见性设置也不能关闭它。当前 `STORY_CONTEXT_PROJECTION_STATE_VISIBILITY_MODE` 默认值及 `.env` 保持不变，没有把两项契约混成一个配置。

直接构造的兼容 planner 仍可使用默认 `require_context_bundle=False` 与 `audit_fallback`；缺包时审计明确标记 `skipped` 或 `rejected`，不记录成功包哈希。`formal_required` 或已发生的受保护预算超限优先于该兼容行为。直接调用 `_record_context_bundle_audit` 仍只返回审计，真正的模型边界由预检和写作入口阻止。

## 失败与数据边界

- 拒绝返回 `context_projection_rejected`，保留原错误、预算详情及父分支绑定的 `context_bundle_rejected` 审计。
- 初次构建失败时不调用结果规划、正文或修复模型；写前规划后才发生故障时，已完成的规划调用不能撤回，但不再调用正文模型。
- 实际回合任务失败，不输出 SSE 正文，不形成可提交 artifact，不改动正式剧情状态。失败记录仍保存于草稿任务审计，便于定位。
- 成功的预检不是正文通过审核；权威状态、事实校验、局部修复、复审和提交顺序继续生效。
- `RULES_VERSION` 升为 `prepared-player-turn/26`，旧准备结果失效后重新生成，已保存分支不迁移、不删除。

适用范围是当前真实模型回合续写。冻结官方开场、开场润色、人物概况和其他独立请求不在本轮验收范围；未宣称所有模型请求都经过此闸门。CLI 可选兼容路径仍保留，未整体迁移。

## 验证

夹具为当前达标官方长篇《太虚遗录》`0.1.3`，102610 汉字，遍历全部官方身份入口验证两种可见性模式的成功构建。故障测试使用临时数据库及离线模型返回值。

覆盖有效声明但缺 resolver、resolver 抛错/空值/错误类型、状态验证标记缺失、受保护预算溢出、planner 复用、写作入口直接调用、兼容模式标记。实际 API 回合任务覆盖初始缺模块、bundle 校验失败、预算超限以及规划后才发生的模块故障，断言无正文流、无 artifact、剧情数据库逐项 dump 不变、失败审计绑定父分支。

核心定向回归 114 项通过（3.291 s）；API 定向 67 项通过（3.072 s）；完整 API 520 项通过（85.896 s）。最终补强已有成功修复用例，在 `require_context_bundle=True` 下跑通两段局部修复和完整复审；该用例与可选兼容用例合计 2 项通过。补强只改变测试参数，没有再修改运行时代码。各组有重叠，不相加计数。`git diff --check` 通过。

```sh
python3 -B -m unittest tests_py.test_context_required tests_py.test_context_bundle tests_py.test_context_memory_longform tests_py.test_context_prompt_integration tests_py.test_state_visibility tests_py.test_action_review_context
.venv-api/bin/python -B -m unittest tests_api.test_context_required tests_api.test_reader_actions tests_api.test_dynamic_memory
.venv-api/bin/python -B -m test_support.run api
git diff --check
```

未运行真实模型、Jev 或生图接口。未改公司中转、`.env`、正式会话、冻结故事包及历史提示基线。没有前端变更，未运行前端测试。未重跑完整 core，既有 `consequences.plan` 提示哈希差异未在本轮处理。

## 后续顺序

1. 为来源选择准备有标签的遗漏/误召回评估，重点检查指代、明确计划来源、旧事实失效和未结义务；避免仅以减少字符数判断有效性。
2. 进行固定次数的真实正文—审核—修复—提交实验，记录原始失败、选择依据及实际耗时；离线调用链通过不替代这一步。
3. 上下文质量验收完成后继续既定剧情配图、生图提示及延迟优化。

上一阶段：[行动审核历史引用与空窗口审查](context-management-action-history-review-2026-09-27.md)。
