# NQ-001 首轮离线更正报告

本报告由保留的首轮脱敏响应、临时 draft SQLite 和主 SQLite 只读重放生成；没有启动 API，也没有新增真实模型调用。原 `report.md`、`report.json` 和 `calls/` 文件保持不变。两次同 request_id 尝试计为一个场景。

## SSE 更正

- 开场：`delta ×3 → done`，含 `session` 和 `branch`，正式提交 `1/1`。
- 告知首次：`delta ×? → reset ×3 → delta → error:generation_failed`，正文只有预览，正式提交 `false`。
- 告知重试：同一 request_id，`delta ×? → reset ×3 → delta → error:generation_failed`，正文只有预览，正式提交 `false`。
- `HTTP 200` 只表示 SSE 传输建立；终端 `error` 优先，流结束没有提交凭证也不会判定成功。

## 更正后的指标

- 首稿通过率：`Unknown`。完整首稿审查通过证据未由公开接口给出，保留 Unknown；不能沿用原报告的 `0/1` 作为统计率。
- 最终回合成功率：`0/1`；两次尝试不是两个场景。
- 事实越权率、审查误拦率、修复成功率：均为 `Unknown` / `Unknown` / `Unknown`。人工缺陷和审查拒绝不能替代总体统计率。
- 首字时间：`Unknown (raw SSE has no event timestamps)`。内部 draft 指标 first_text_ms 为 `[18325, 15905]` ms，和 SSE 首字时间不是同一口径。
- HTTP 完整响应时间：`[65210.049, 57242.725]` ms；内部生成 complete_ms 为 `[65060, 57044]` ms。
- 供应商报告用量：`32` 次、`211428` tokens（首次 17/109690，重试 15/101738）；未把 raw JSON 行数当作调用次数。估算值：Unknown。

## 32 次调用阶段分布

| 阶段 | 次数 |
| --- | ---: |
| `action_authority` | 4 |
| `action_review` | 6 |
| `chapter` | 2 |
| `fact_extraction` | 6 |
| `local_repair` | 4 |
| `result_contract` | 4 |
| `scene_grounding` | 6 |

规划和正文生成完成；事实提取、行动审查和场景落地完成；第一次失败闸门是 `validation/full_review`。局部修复调用本身返回完成，但修复验证仍 pending/failed。没有到达状态校验或原子提交，HTTP 层没有传输异常证据。

## 状态与根因边界

主 SQLite 仍为一个 root `branch_nodes`、零 `turn_requests`、零 `game_events`；两次失败均未写子分支或改变权威状态。

会话1应优先处理：**场景全审查拒绝无来源的 NPC 背景/否定性知识断言，而现有局部修复循环没有产出可通过的最终正文**。证据支持这是首个失败闸门和最终失败链条；尚不能仅凭公开响应断言是提示、模型还是修复预算的单一根因。需要的最小新增观测是每次 planner/reviewer/repair 的脱敏 request id、stage、review 结果和最终 artifact 校验结果。

## 原报告结论变化

- 保持：开场成功、告知最终失败、两次尝试一条场景、211428 supplier-reported tokens、无子分支/权威状态写入、询问和等待未执行。
- 更正：评估器现在读取 top-level `entries`，按独立 `event:`/多行 `data:`/空行边界解析 SSE；预览正文不再被当作正式正文。
- 更正：首稿通过率改为 Unknown；不能从缺少完整阶段审查字段的记录推导 `0/1`。
- 仍为 Unknown：事实越权率、审查误拦率、修复成功率、方向可用时间和 SSE 首字时间。
