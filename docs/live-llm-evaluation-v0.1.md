# 真实 LLM 评估 v0.1

## 目的

`npm run evaluate:live` 用少量真实模型调用验证共创链路的运行时边界。它不是默认测试，也不替代 `npm test`、`npm run typecheck` 或 StoryPackage 校验。

迁移期 Python 对等入口是 `python3 -m open_story_engine evaluate-live`（或 `npm run py:evaluate:live`）。它只覆盖 Python 当前实现的宽泛目标场景；完整六场景 TypeScript 评估在 Python 完整替换前仍是行为基线。

命令只加载固定的原创 StoryPackage，并在内存 SQLite 会话中运行；默认不写入项目数据库、不会打印或保存模型原始输出。仅当调用方显式设置 `STORY_LIVE_EVALUATION=1` 后才会访问 `.env` 中配置的模型端点。为将剧情行为验收与中转站 SSE 传输波动区分开，评估请求使用普通 JSON 响应并允许单次等待 60 秒；玩家 CLI 仍默认使用 SSE 逐段展示。

```bash
STORY_LIVE_EVALUATION=1 npm run evaluate:live
```

Python 的单场景授权验收使用独立内存会话：

```bash
STORY_PLANNER=openai \
STORY_LIVE_EVALUATION=1 \
STORY_LIVE_EVALUATION_MAX_CALLS=4 \
python3 -m open_story_engine evaluate-live \
  --scenario broad_goal_starts_current_phase \
  --output /private/tmp/open-story-engine-python-live.json
```

可用 `STORY_LIVE_EVALUATION_MAX_CALLS` 设置总调用上限，默认值为 `8`，允许范围为 `1` 至 `20`。模型重试也计入上限。需要保留脱敏报告时，可附加 `--output data/live-evaluation-report.json`；报告包含场景名、通过状态、检查项、调用次数、错误摘要，以及每次调用的操作阶段、重试原因、耗时、响应模式、HTTP 状态和失败类别。报告不保存模型原文、请求正文或密钥。指定 `--output` 时，每完成一个场景和每开始或结束一次模型调用都会更新报告；中断前的报告会标为 `runStatus: "incomplete"`，并保留 `inFlightCall`，不得当作完整验收结论。

排查或回归单个问题时可附加 `--scenario <场景 ID>`，只执行指定场景，避免重复调用已完成的场景。

## 场景

1. `canonical_route_skips_model`：规范节点复用不应调用模型。
2. `broad_goal_starts_current_phase`：宽泛目标经 LLM 锚定为救援优先，动态正文以 `storyArc.started` 的当前阶段开始，不能提前封章。
3. `locked_signal_room_state`：排水后水位降低，但信号室仍锁闭、唐栖仍未获救。
4. `controlled_rejoin_uses_new_narration`：折返取证满足汇合状态，但正文不得拼接未展示原著节选。
5. `free_text_request_idempotency`：同一 `requestId` 不重复调用方向评估器或追加节点。
6. `forbidden_supernatural_action`：超自然行动被拒绝，并引用 `fact_no_supernatural`。

六个场景在无重试时共使用六次模型调用。任何场景失败时命令以非零状态退出，但仍输出其余场景结果，便于判断是模型质量、输出格式还是服务层约束导致的问题。

## 判定边界

六个场景全部通过时，结论仅代表本次真实模型调用已满足 schema、引用范围、叙事事实、地点路线、状态补丁、受控汇合与请求幂等性约束。任一场景失败则本次验收不通过，并保留失败场景和错误摘要。它不推断读者偏好、长期节奏或发布级文学质量，也不证明玩家 CLI 的 SSE 逐段展示；这些不属于当前剧情行为验收范围。
