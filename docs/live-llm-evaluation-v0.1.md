# 真实 LLM 评估 v0.1

## 目的

`python3 -m open_story_engine evaluate-live` 用固定场景验证共创链路的运行时边界。它不是默认测试，也不替代 Python 自动回归、StoryPackage 校验或玩家 CLI 的 SSE 人工阅读验收。

评估只加载固定的原创 StoryPackage，并为每个场景新建内存 SQLite 会话；不会写入 `STORY_DATABASE_PATH` 指向的项目数据库。评估报告不包含模型原文、请求正文或密钥。

为将剧情行为验收和中转站 SSE 波动分离，`evaluate-live` 忽略 `STORY_LLM_STREAM` 与 `STORY_LLM_TIMEOUT_SECONDS`，固定使用普通 JSON、单请求 60 秒、禁用 JSON/SSE 传输降级。玩家 CLI 的 SSE 优先与 JSON 回退逻辑不受影响，应单独人工验收。

## 命令

先在 `.env` 或当前 shell 设置 `STORY_LLM_BASE_URL`、`STORY_LLM_API_KEY`、`STORY_LLM_MODEL`，然后执行：

```bash
STORY_PLANNER=openai \
STORY_LIVE_EVALUATION=1 \
STORY_LIVE_EVALUATION_MAX_CALLS=8 \
python3 -m open_story_engine evaluate-live \
  --output /private/tmp/open-story-engine-python-live.json
```

`STORY_LIVE_EVALUATION_MAX_CALLS` 默认是 `8`，可设为 `1` 至 `20`。报告只把带有 transport 记录的 HTTP 请求计入调用次数；本地归一化、状态拒绝和审计记录不计为模型调用。若累计调用数已达到上限，后续需要模型的场景标为 `not_run`，离线场景仍会执行。

传入 `--scenario <场景 ID>` 时只运行该场景：

```bash
STORY_PLANNER=openai \
STORY_LIVE_EVALUATION=1 \
python3 -m open_story_engine evaluate-live \
  --scenario locked_signal_room_state \
  --output /private/tmp/open-story-engine-locked-room.json
```

指定 `--output` 后，命令会先写入 `runStatus: "incomplete"`，并在每个场景完成后更新报告。中断时已完成场景会被保留，不能视为完整验收结论。

## 场景

| 场景 | 是否调用真实模型 | 通过标准 |
| --- | --- | --- |
| `canonical_route_skips_model` | 否 | 规范方向复用原著正文，Planner 不被调用。 |
| `broad_goal_starts_current_phase` | 是 | 宽泛自由文本锚定为救援优先，进入当前阶段并通过正文状态校验。 |
| `locked_signal_room_state` | 是 | 信号室保持锁闭、唐栖仍为已定位未获救，正文通过锁闭状态守卫。 |
| `controlled_rejoin_uses_new_narration` | 否 | 折返取证满足声明的汇合条件，正文不拼接未展示的原著节选。 |
| `free_text_request_idempotency` | 否 | 相同 `requestId` 不重复方向判定、不重复调用 Planner、不追加分支。 |
| `forbidden_supernatural_action` | 否 | 超自然行动被拒绝，并引用 `fact_no_supernatural`。 |

真实模型场景可能因短稿续写而发起额外 JSON 请求；这些请求同样计入上限。评估不会为了凑足调用数而重复生成。

## 判定边界

全部场景通过且实际调用数未超过上限时，结论仅代表本次调用满足 schema、引用范围、叙事事实、地点路线、状态补丁、受控汇合、请求幂等性和世界禁则。任一场景失败或未运行则本次验收不通过。

它不证明玩家 SSE 的逐段展示、读者偏好、长期节奏或发布级文学质量。SSE 人工阅读验收与 2,000 字正文质量检查应使用隔离的试玩数据库单独执行。
