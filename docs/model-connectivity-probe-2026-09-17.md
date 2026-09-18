2026-09-17 GPT-5.6 Sol 模型连通性探针

## 配置范围

- `STORY_PLANNER=openai`
- `STORY_LLM_MODEL=gpt-5.6-sol`
- 使用仓库 `.env` 中已配置的 OpenAI-compatible 地址和 API key；本记录不保存密钥或完整地址。
- `STORY_LLM_TIMEOUT_SECONDS=120`
- 原配置使用 `STORY_LLM_STREAM=true`、`STORY_LLM_TRANSPORT_FALLBACK=false`。

## 探针结果

| 传输 | 结果 | 证据 |
| --- | --- | --- |
| SSE JSON | 未完成 | 首段数据约 16.1 秒未返回，网关报 `transport_error`，无 HTTP 状态码。 |
| 非流式 JSON | 通过 | HTTP 200，返回 `{"ok":true,"model":"gpt-5.6-sol"}`，耗时约 84.4 秒。 |

## 判定

当前可以确认模型、密钥和接口具备基本调用能力；不能确认交互试玩可用。GPT-5.6 Sol 在当前中转链路上的非流式响应过慢，SSE 又未在首段超时窗口内提供增量，尚不能进入连续真实试玩验收。

这次探针没有创建会话、分支或数据库写入。后续应先确定中转是否支持稳定 SSE 首段，再用同一故事包执行开场、告知、询问、等待和偏离恢复；仅把 `STORY_LLM_STREAM` 改为 `false` 会牺牲正文首字体验，不能直接视为修复。

## 客户端侧后续调整

在不改公司中转站的前提下，应用网关已将结构化 `complete_json` 请求固定为非流式，正文 `complete_text` 继续按配置使用 SSE；新增 `STORY_LLM_FIRST_DELTA_TIMEOUT_SECONDS`，默认 30 秒，仅控制正文 SSE 首段等待。每次请求的观测现在记录 `responseHeadersMs`、`firstEventMs`、`firstContentMs` 和 `completeBodyMs`（可获得时），用于区分连接建立、首段内容和完整响应耗时。原始探针结果仍是历史证据，尚未替代真实桌面连续试玩。

### 调整后的复测

- 同一 GPT-5.6 Sol 配置，非流式 JSON，`max_tokens=128`，无数据库写入。
- HTTP 200，完整耗时约 10.7 秒；`responseHeadersMs=10667`、`completeBodyMs=10668`。
- 结果仍说明等待主要发生在响应头返回前，但与此前约 84.4 秒样本相比波动很大；不能把单次探针当作稳定性结论。

### 单回真实开场复测

使用 `taixu-relics-part1@0.1.2`、首个官方身份、临时 SQLite 和单次模型调用上限，未写正式存档：

| 请求 | 结果 | 观测 |
| --- | --- | --- |
| `STORY_LLM_STREAM=true` | 失败 | 约 16.3 秒后返回 `empty_stream`；重试被单次调用上限拦截。 |
| `STORY_LLM_STREAM=false`，45 秒 | 失败 | 约 45.0 秒 `transport_error`，未完成开场。 |
| `STORY_LLM_STREAM=false`，120 秒，`max_tokens=4096` | 失败 | 约 120 秒仍未收到响应头；未形成分支。 |

这说明当前长篇开场的主要阻塞不是单纯的 SSE 开关：短 JSON 探针可以在 10.7 秒完成，但带真实长提示和较大输出预算的请求可能长时间没有响应头。下一轮应按阶段单独缩小开场／正文的输出预算和上下文负载，再以完整开场质量为门槛验证，不能把超时简单改成无限等待。

## 新的验收原则

中转站慢本身不再作为单独失败条件。可接受的实现必须满足：请求提交后立即显示真实的等待状态；模型开始返回正文后持续展示已收到的正文；最终正文通过事实、状态、篇幅和人工可读性检查后才写入分支。等待期间不能伪造正文或进度，超过预算没有真实正文时明确失败并允许重试，且不产生半成品分支。
