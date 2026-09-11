## 决策状态

- 实施日期：2026-09-10。
- 状态：已实施并通过接口测试与真实模型联调；前端第一版写作交互依赖本批接口。
- 范围：在玩模式（play mode）下提供会话创建、回合续写、流式展示与存档删除。草稿编辑与确认流仍属于后续批次；[读取 API 服务](api-read-service-v0.1.md)的只读契约不变，默认启动仍是只读。

## 启用方式

只读模式保持不变。写作模式通过环境变量显式开启，并会读取仓库根目录的 `.env` 获取模型配置（与 CLI 共用同一组 `STORY_LLM_*` 变量；只读模式仍不读取 `.env`）：

```bash
STORY_API_PLAY=1 \
  .venv-api/bin/python -m uvicorn open_story_engine.api:create_app --factory --host 127.0.0.1 --port 8000
```

`STORY_PLANNER=mock` 时使用 MockPlanner（仅结构测试草稿，不代表可读性验收）；`openai` 时按 CLI 相同参数构建真实 Planner。针对响应缓慢的端点，建议附加（进程环境变量优先于 `.env`）：

```bash
STORY_LLM_STREAM=true STORY_LLM_TRANSPORT_FALLBACK=false \
STORY_LLM_TIMEOUT_SECONDS=120 STORY_LLM_REASONING_EFFORT=none
```

早期长正文联调曾因 SSE 静默、推理预算耗尽而改用 JSON；网页开场与长篇续写已通过真实 SSE 输出验证，当前推荐以上流式配置。`reasoning_effort=none` 仅适用于支持该扩展字段的端点。

健康检查在玩模式下返回 `phase: "play"` 与 `generation_available` / `state_updates_available`；配置不全（如缺少模型密钥）时接口仍可浏览，写入请求返回 `503 planner_unavailable`。

## 写入接口

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/v1/sessions` | 创建共创会话并写入入口根分支，返回 `{ session, branch }`（201）。支持原著角色或新角色；`identity_opening=true` 时调用真实模型生成第二人称开场，校验后保存。 |
| `POST /api/v1/sessions/{session_id}/branches` | 回合续写：`parent_branch_id` + `direction_id`（已公布方向）或 + `text`（自由行动，二者互斥），返回 `{ status, request_id, deduplicated, branch, kind, reason }`。 |
| `POST /api/v1/sessions/{session_id}/branches/stream` | 相同请求体，以 SSE 返回 `delta`、`reset`、`done` 或 `error` 事件，正文边生成边展示。 |
| `POST /api/v1/import/novel` | 导入标准 UTF-8 TXT：网页提交 `{ file_name, source_text }`，后端自动生成内容标识与默认版本；相同正文再次导入复用已有包。构建及双审计通过后，全部文件写入完成才发布到书库，失败不暴露半成品。返回包引用与标题、章节、角色和剧情数量，前端直接跳转到剧情与角色选择页。兼容显式 `package_id`、`version` 调用，显式引用重复仍返回 `409 package_exists`。不调用模型；无效文件返回 `422`。 |

行为契约：

- 全部经过 `CoCreationService`：方向必须来自父分支已公布菜单；自由文本先经本地方向判定；正文通过本地事实与状态校验后才保存分支。生成失败不产生新正文分支，已经完成的方向判定可保留供同一请求恢复。Web 采用第二人称限知视角，开场600—1000字，续写2000—2500个非空白字符，分段生成与校验；不额外调用模型进行完整原著事实审阅。
- `request_id` 幂等：同一请求重发返回同一分支并标记 `deduplicated: true`；同一 `request_id` 换父分支/方向/文本返回 `409`。
- 不存在的会话返回 `404 session_not_found`；会话绑定的故事包不可加载时写入返回 `409 package_binding_changed`；入口与角色不匹配返回 `409 invalid_entry`。
- 2026-09-10 产品决策：Web 玩家可用任意原著角色 × 任意剧情入口开局（`normalize_entry_selection(..., allow_any_source_character=True)`，核心为可选开关，CLI 默认行为不变）；包内每个入口声明的角色仅作引导建议，新建角色路径仍走完整档案校验。
- 所有写入在应用内串行执行（单进程锁）；SQLite 单写者约束下不保证高并发吞吐，第一版面向本机使用。
- 只读模式（未开启 play）下以上路径返回 `404 endpoint_unavailable`。

## 验证

```bash
.venv-api/bin/python -B -m unittest discover -s tests_api -v      # 55 项 API 回归
python3 -B -m unittest discover -s tests_py -q                    # 188 项核心回归
git diff --check
```

- `tests_api/test_import_api.py`：真实《雨夜候车室》TXT 自动导入、中文文件名、完整包与模块双审计、4 位角色开局、重复导入无重写、非法文件、审计失败与中断写入不发布半成品。全部使用临时目录与 MockPlanner。
- `tests_api/test_play_api.py`：玩模式健康标识、只读模式拒绝写入、开局创建与根分支、非法角色 409、方向续写与幂等去重、request_id 冲突 409、自由文本自定义方向写入、无效方向 409 且不落库、未知会话 404。全部使用临时目录与 MockPlanner。
- 真实模型端到端（2026-09-10，`deepseek-v4-flash:cloud`，JSON/120s/reasoning none）：前端「开始游戏」创建会话 → 选择大方向（本地 `arc_selection`，即时返回）→ 选择小方向，约 90 秒生成一页偏离原著的正文并落库；两次失败尝试（SSE 静默超时、推理截断）均未写入任何进度。该记录是一次性联调结果，不替代[真实模型验收](live-llm-evaluation-v0.1.md)。

## 遗留

- 编辑后确认（draft_editor 跨请求版）、跨进程生成任务恢复和流式事件历史回放尚未实现。
- CLI 与 API 各自构建模型运行时；Web 的限知叙事与篇幅策略在独立的 PlayerNarrativePlanner 中实现，CLI 默认行为保持原样。

## 流式正文与重试

`POST /api/v1/sessions/{session_id}/branches/stream` 仅在 play 模式开放，请求体与普通续写一致。响应为 `text/event-stream`：

- `delta`：`{"text":"正文片段"}`，前端追加到当前预览。
- `reset`：`{}`，模型重试或传输切换时清空旧预览。
- `done`：完整 `PlayContinueResponse`，只有返回的已保存分支才作为新进度。原著直接复用和幂等命中可能只有 `done`，不伪造打字动画。
- `error`：`{"code":"generation_failed","status":503,"message":"..."}`，流内失败以事件表达；前端恢复原页并允许重试，不能仅以 HTTP 200 判断生成成功。

连接使用心跳与有界队列。浏览器断开后，当前进程中的生成可继续完成保存；同一 `request_id` 重试复用结果。若自由行动已判定、正文尚未成功，则恢复正文生成，不重复登记行动。进程退出后的事件回放与自动恢复不在本轮范围。客户端同时验证 UTF-8 分段、结束事件和流中断，避免把半篇正文当作存档。

## 身份开场

创建请求可带 `identity_opening: true`（默认 false，网页开局启用）。`api_openings.py` 提供只在创建该会话时使用的入口视图，不改写故事包文件或已有存档。原始接口与 CLI 默认开局保持原有语义。

《雨夜候车室》的四份开场按小说源文本 SHA-256 绑定，分别提供正文、标题、摘要、待处理问题、地点和 `openingActions`。角色地点必须存在于故事包；会话初始状态与根分支的玩家位置、角色位置映射保持一致。其他导入小说使用自身角色资料和声明入口的原文，不套用示例人物情节。

开场作为 `source_entry` 根分支保存并标记 `canonicalRelation: diverged`，后续请求从存档中的角色、状态和正文继续。`openingActions` 是可见的自然语言行动，点击仍走自由行动判定与流式续写，不虚构原著方向 ID。阅读页不会用整章原文覆盖这一开场。旧存档不自动迁移。

## 删除游戏存档

`DELETE /api/v1/sessions/{session_id}` 仅在 play 模式开放；成功返回 `204`，不存在返回 `404 session_not_found`。前端先展示不可恢复的删除确认，再提交请求。

删除与同一应用实例的续写共用写锁。存档、分支、方向评估、事件、叙述、派生故事包和审计记录在同一 SQLite 事务中删除；固定故事包与其他存档保留。失败会回滚全部删除。无需故事包仍可加载或模型配置可用；数据库不存在时返回 404，不创建数据库。读取模式下不开放该路由。


## 2026-09-11 玩家体验接口

- `POST /api/v1/sessions/stream`：请求体同会话创建，SSE 事件同续写；网页提交 `identity_opening: true`。开场完成校验后才创建存档。`request_id` 固定映射到会话，同 ID 与同开场参数重试返回原结果，参数冲突返回409。
- `POST /api/v1/sessions/{session_id}/rename`：`{"title": "名称"}`，去除首尾空白后1—80字符。名称存在独立的 `session_play_preferences`，不改角色契约和小说原件。
- `GET /api/v1/sessions/{session_id}/journey?branch_id=...`：只读返回角色目标、路线阶段进度、当前任务、人物、可见线索、最多两条最新状态变化、所选路线的选择回顾。旧数据库缺少偏好表时只读兼容，不初始化数据库。
- `POST /api/v1/sessions/{session_id}/end`：`{"branch_id": "..."}`，只允许在叶节点主动结束路线；已有后续返回409。已结束路线不能继续追加，但更早的节点仍能产生另一条分支。重复已完成续写请求仍能恢复原结果。
- 存档列表新增 `title`、`role_name`、`recent_progress`、`updated_at`；删除存档一并删除偏好记录。
- `reset` 表示当前片段需要修订或传输恢复。前端清空预览后，服务端重新发送已通过的段落，再发送本段新增文字。`done` 中保存的正文是最终读取依据。

目标百分比当前是确定性的路线阶段进度，未声称模型能自动判断每个角色的全部通关条件。只有源包声明的结局可以自动收尾，未自行发明角色死亡/失败规则。
