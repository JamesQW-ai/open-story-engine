# 永久损毁道具最终定向真实模型复测

本报告只统计显式设置 `STORY_PLANNER=openai` 后的真实模型尝试。基线为 P0 checkpoint `c4f4d44`，模型 `deepseek-v4-flash`，StoryPackage `taixu-relics-part1@0.1.3`，请求为“我永久损毁道具：引荐文书。”，没有显式重试。

结论：本轮未通过，真实请求在 `branch_planner` 合约校验阶段失败。SSE 只有连接、心跳和 `error`，没有正文、`done` 或 `status=written`。`action_authority`、正文生成、`full_review`、`repair` 和原子提交没有形成可接受结果。

真实 planner 两次 `result_contract` 调用均返回 `decision=ready`，提出 `item_open_letter.destroyedPermanently=true`，其依据是当前持有关系；`scenePlan.knowledge` 为空，未将持有事实当作已经损毁的证据。叙事候选位于 `narrativeOptions[]`，并标为 `pending + requiresConfirmation=true`。

边界检查仍未通过。第一次响应在 `method` 中写入“撕扯、揉搓”，并断言守门弟子会目睹。第二次响应删除了这些具体动作，但仍在 `method` 中写入“亲手损毁”并断言守门弟子会目睹；具体动作才被放入待确认选项。随后本地 planner 合约报错：`永久损毁的具体动作须待确认，不能写入行动计划或权威状态：抽出`。因此不能把 planner 或 K1 记为通过，也不能据此继续判断正文审查或提交。

真实尝试共 2 次供应商调用，报告 `20758` tokens，完整耗时 `9632ms`，首字时间为 `Unknown`；两次调用属于同一 request，不是两个场景。未确认草稿保留为 `text=""`、`status=unconfirmed`、正文哈希 `null`。原始 planner 响应、调用观察和失败原因见 `calls/0022-real-turn-drafts.json`，SSE 见 `calls/0016-real-turn.sse`。

失败前、失败后及停止服务再启动后的带 `branch_id` 读取均确认父分支仍为 `state_version=1`，`item_open_letter` 仍由陆照临持有，未出现 `destroyedPermanently`。真实尝试没有创建新分支、游戏事件或事件叙述。此前同一数据库中因未设置 `STORY_PLANNER=openai` 而产生的 mock 子分支 `branch_b47d4af8-4555-4363-ae5d-6030052ddf1e` 已保留在证据中，但不计入本次真实模型结果；其正文与请求不一致，不能作为成功。

完整脱敏证据位于本目录 `calls/`，包括真实请求、SSE、前后权威状态、分支视图、用量、数据库导出、planner 原始结果和重启读取。没有修改运行时代码、提示、StoryPackage 或数据库，没有合并 main 或推送远端。
