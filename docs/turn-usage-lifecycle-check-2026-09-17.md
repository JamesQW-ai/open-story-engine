## 验收范围

本轮核对准备任务在失败重试、迟到回包、取消、离页、缓存复用和正文展示回执之间的用量归属。测试只枚举当前达标官方长篇《太虚遗录》第一部（`taixu-relics-part1@0.1.2`，102610 CJK），没有使用已停用的《雨夜候车室》，没有调用真实模型，也没有改写正式存档。

## 核对结果

- 失败后重试保留 `previous_attempts`，每次尝试的调用次数、报告 token、未报告调用、排队和执行耗时分别保留；缓存命中不增加尝试或调用。
- 运行中的旧供应商回包只补入历史尝试，不能覆盖替代任务的正文、状态或用量；替代任务在旧回包到达前保持 `unmeasured_attempts`，回包到达后才解除未知。
- 排队阶段取消、服务关闭和订阅全部离开明确记录零调用；已经进入生成但缺少供应商回执的任务保持 `tokens: null`，不把未知成本记成零。
- 生成完成但未选择或未展示的草稿保留已知用量；阅读回执只确认对应分支，重复回执不重复计费，跨会话、跨分支和已淘汰记录不能伪造确认。
- 只读用量接口不初始化或迁移数据库；历史记录缺少完整字段时报告 `unmeasured_attempts`，累计 token 保持未知。

## 验证

使用 API 虚拟环境执行：

```text
.venv-api/bin/python -B -m unittest tests_api.test_longform_usage tests_api.test_longform_draft_lifecycle tests_api.test_longform_reading_receipts tests_api.test_longform_saved_choice_reuse tests_api.test_longform_repair_fallback
```

结果：44 项通过。覆盖失败回执、重试重启、迟到回包、取消后用量、任务淘汰、共享订阅、展示回执、修复失败留痕和可读正文降级。

## 保留边界

`TurnDrafts` 仍是有界的私有草稿记录，受 600 秒有效期和 128 项容量限制；任务被清理后不承诺恢复历史成本。供应商未报告的底层重试成本、进程中断后无法取得的回执仍为 `unknown`。这些统计是网关回执和本地耗时证据，不是实际账单，也不代表正文质量验收。NQ-001、END-001、SRC-001 继续按计划暂缓。
