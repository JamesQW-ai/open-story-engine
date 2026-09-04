# 真实 LLM 批量验收偶发超时

## 状态

Closed。两次独立的完整真实模型回归均通过；该结论不改变本地确定性测试与 SSE 手工展示仍需分别验证的边界。

## 现象与证据

在 `rainy-waiting-room@0.1.0`、`deepseek-v4-flash:cloud` 上执行以下命令：

```bash
STORY_LIVE_EVALUATION=1 STORY_LIVE_EVALUATION_MAX_CALLS=8 npm run evaluate:live
```

六场景完整回归得到 `5/6`：`locked_signal_room_state` 的两次 Planner 请求均在 60 秒内超时，其余五项通过。该结果只记录场景、调用次数与错误摘要，未保存模型原始输出。

同一代码版本下，以下定向命令在一次 Planner 调用中通过，确认“排水后水位降低、信号室仍锁闭、唐栖仍未获救”的状态与正文约束可正常成立：

```bash
STORY_LIVE_EVALUATION=1 STORY_LIVE_EVALUATION_MAX_CALLS=2 npm run evaluate:live -- --scenario locked_signal_room_state
```

因此当前证据指向公司模型端点在连续请求时的可用性波动，而不是该场景的规则、地点路线或状态断言错误。

## 影响范围

- 完整真实模型回归暂不能作为稳定通过证据。
- 单场景真实验收仍可用于验证具体剧情约束。
- `co-create` 的 SSE 逐段展示不由本问题的非流式验收覆盖，仍需单独观察。

## 当前缓解

- `evaluate:live` 使用普通 JSON 响应并将单次等待限制为 60 秒，避免把 SSE 传输格式混入剧情行为结论；报告会记录每次调用的耗时、HTTP 状态、响应模式、失败类别与重试原因，但不记录模型原文。
- `evaluate:live -- --scenario <场景 ID>` 支持仅重跑失败场景，避免重复消耗已经通过的调用。
- Planner 的确定性状态和叙事实守卫继续拒绝不合格正文；超时不创建分支，也不被当作剧情通过。

## 2026-09-04 复验

在同一 `rainy-waiting-room@0.1.0` 与 `deepseek-v4-flash:cloud` 配置下，已完成两次独立的完整六场景验收：

- 第一次：`6/6` 通过，实际 6 次调用；全部为 HTTP `200` 的普通 JSON 响应，无重试、超时、缺正文或长度截断。
- 第二次：`6/6` 通过，实际 7 次调用；全部为 HTTP `200` 的普通 JSON 响应。`controlled_rejoin_uses_new_narration` 的第一次模型输出未通过本地内容校验，因此以 `model_output_rejected` 原因重试一次后通过；没有超时、缺正文或长度截断。

第二次的脱敏报告仅保存在临时目录，未写入项目数据库或 Git 工作区。中途曾有未返回终端回执的运行，因此不计入上述两次证据；增量报告随后被增强为在中断时保留 `runStatus: "incomplete"` 和在途调用信息。

## 关闭标准

已满足：在同一 StoryPackage 与模型配置下，两次独立的完整六场景验收均在每次最多 8 次调用内通过，且没有超时、流式缺正文或长度截断错误。若后续复现超时，应恢复为 Open，并先依据不含正文的请求时长、HTTP 状态与重试原因观测判断是端点配额、网关超时还是调用编排问题。
