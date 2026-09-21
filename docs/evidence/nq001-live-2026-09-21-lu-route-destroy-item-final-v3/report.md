# 永久损毁道具最终真实模型复测（7103065）

本轮使用真实模型配置 `STORY_PLANNER=openai`，模型为 `deepseek-v4-flash`，StoryPackage 为 `taixu-relics-part1@0.1.3`，请求固定为“我永久损毁道具：引荐文书。”，没有显式重试。

结论：本轮未通过。`targetCjk` 的修复已生效：供应商返回动态区间 `[80,250]`，未触发 scalar `120` 归一化路径；但首个失败转为 `branch_planner` 的 canonical/pending 字段边界校验。

失败审计列出以下字段泄漏：`$.method`、`$.steps[0].action`、`$.stateChanges[0].reason`、`$.scenePlan.outcome`、`$.scenePlan.lengthReason`、`$.scenePlan.beats[0].purpose`、`$.scenePlan.observationLimits[0]`、`$.scenePlan.observationLimits[1]`。这些字段写入了抽出、对折、撕扯、撕碎以及守门弟子目睹或可能反应。具体动作候选同时出现在 `narrativeOptions[]`，并标为 `pending + requiresConfirmation=true`，但这不能抵消 canonical 字段泄漏。

planner 仅提出一个 `item_open_letter.destroyedPermanently=true` 状态变化，持有关系作为依据，`scenePlan.knowledge=[]`；没有把持有证据当作已损毁证据。但因为 canonical plan 校验失败，planner 和 K1 没有形成最终通过结果。`action_authority` 未执行，无法确认其输入是否剔除了 `narrativeOptions[]`，记为 `Unknown`。

SSE 返回 `generation_failed` / HTTP 503，没有 `done`、正文、`full_review`、`repair` 或原子提交。真实模型调用 2 次，供应商报告 `21279` tokens，完整耗时 `7584ms`，首字时间为 `Unknown`；未确认正文为空。

失败前、失败后及服务重启后的带 `branch_id` 读取均确认父分支 `state_version=1`，引荐文书仍由陆照临持有且未标记永久损毁；本轮没有新增真实分支、游戏事件或事件叙述。

完整脱敏证据位于本目录 `calls/`：请求和 SSE 为 `0005-turn-request.json`、`0005-turn.sse`；planner 原始 JSON、规范化审计、错误字段和未确认草稿为 `0010-turn-drafts.json`；失败后和重启读取为 `0006`、`0007`、`0012`、`0013`；用量为 `0008`、`0014`；数据库盘点为 `0011-db-*.json`。

本轮没有修改运行时代码、提示、StoryPackage 或数据库，没有合并 main 或推送远端。
